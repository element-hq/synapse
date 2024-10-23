#!/bin/python3
"""Generate Synapse documentation from JSON Schema file."""

import json
import re
import sys
from typing import Any, Optional

import yaml

HEADER = """<!-- Document auto-generated by scripts-dev/gen_config_documentation.py -->

# Configuring Synapse

This is intended as a guide to the Synapse configuration. The behavior of a Synapse instance can be modified
through the many configuration settings documented here — each config option is explained,
including what the default is, how to change the default and what sort of behaviour the setting governs.
Also included is an example configuration for each setting. If you don't want to spend a lot of time
thinking about options, the config as generated sets sensible defaults for all values. Do note however that the
database defaults to SQLite, which is not recommended for production usage. You can read more on this subject
[here](../../setup/installation.md#using-postgresql).

## Config Conventions

Configuration options that take a time period can be set using a number
followed by a letter. Letters have the following meanings:

* `s` = second
* `m` = minute
* `h` = hour
* `d` = day
* `w` = week
* `y` = year

For example, setting `redaction_retention_period: 5m` would remove redacted
messages from the database after 5 minutes, rather than 5 months.

In addition, configuration options referring to size use the following suffixes:

* `K` = KiB, or 1024 bytes
* `M` = MiB, or 1,048,576 bytes
* `G` = GiB, or 1,073,741,824 bytes
* `T` = TiB, or 1,099,511,627,776 bytes

For example, setting `max_avatar_size: 10M` means that Synapse will not accept files larger than 10,485,760 bytes
for a user avatar.

## Config Validation

The configuration file can be validated with the following command:
```bash
python -m synapse.config read <config key to print> -c <path to config>
```

To validate the entire file, omit `read <config key to print>`:
```bash
python -m synapse.config -c <path to config>
```

To see how to set other options, check the help reference:
```bash
python -m synapse.config --help
```

### YAML
The configuration file is a [YAML](https://yaml.org/) file, which means that certain syntax rules
apply if you want your config file to be read properly. A few helpful things to know:
* `#` before any option in the config will comment out that setting and either a default (if available) will
   be applied or Synapse will ignore the setting. Thus, in example #1 below, the setting will be read and
   applied, but in example #2 the setting will not be read and a default will be applied.

   Example #1:
   ```yaml
   pid_file: DATADIR/homeserver.pid
   ```
   Example #2:
   ```yaml
   #pid_file: DATADIR/homeserver.pid
   ```
* Indentation matters! The indentation before a setting
  will determine whether a given setting is read as part of another
  setting, or considered on its own. Thus, in example #1, the `enabled` setting
  is read as a sub-option of the `presence` setting, and will be properly applied.

  However, the lack of indentation before the `enabled` setting in example #2 means
  that when reading the config, Synapse will consider both `presence` and `enabled` as
  different settings. In this case, `presence` has no value, and thus a default applied, and `enabled`
  is an option that Synapse doesn't recognize and thus ignores.

  Example #1:
  ```yaml
  presence:
    enabled: false
  ```
  Example #2:
  ```yaml
  presence:
  enabled: false
  ```
  In this manual, all top-level settings (ones with no indentation) are identified
  at the beginning of their section (i.e. "### `example_setting`") and
  the sub-options, if any, are identified and listed in the body of the section.
  In addition, each setting has an example of its usage, with the proper indentation
  shown.
"""
INDENT = "  "


def indent(text: str, first_line: bool = True) -> str:
    """Indents each non-empty line of the given text."""
    text = re.sub(r"(\n)([^\n])", r"\1" + INDENT + r"\2", text)
    if first_line:
        text = re.sub(r"^([^\n])", INDENT + r"\1", text)

    return text


def em(s: Optional[str]) -> str:
    """Add emphasis to text."""
    return f"*{s}*" if s else ""


def a(s: Optional[str], suffix: str = " ") -> str:
    """Appends a space if the given string is not empty."""
    return s + suffix if s else ""


def p(s: Optional[str], prefix: str = " ") -> str:
    """Prepend a space if the given string is not empty."""
    return prefix + s if s else ""


def resolve_local_refs(schema: dict) -> dict:
    """Returns the given schema with local $ref properties replaced by their keywords.

    Crude approximation that will override keywords.
    """
    defs = schema["$defs"]

    def replace_ref(d: Any) -> Any:
        if isinstance(d, dict):
            the_def = {}
            if "$ref" in d:
                # Found a "$ref" key.
                def_name = d["$ref"].removeprefix("#/$defs/")
                del d["$ref"]
                the_def = defs[def_name]

            new_dict = {k: replace_ref(v) for k, v in d.items()}
            if common_keys := new_dict.keys() & the_def.keys():
                print(
                    f"WARN: '{def_name}' overrides keys '{common_keys}'",
                    file=sys.stderr,
                )
            return {**new_dict, **the_def}
        elif isinstance(d, list):
            return [replace_ref(v) for v in d]
        else:
            return d

    return replace_ref(schema)


def sep(values: dict) -> str:
    """Separator between parts of the description."""
    # If description is multiple paragraphs already, add new ones. Otherwise
    # append to same paragraph.
    return "\n\n" if "\n\n" in values.get("description", "") else " "


def type_str(values: dict) -> str:
    """Type of the current value."""
    if not (t := values.get("type")):
        return ""
    if not isinstance(t, list):
        t = [t]
    return f"({"|".join(t)})"


def items(values: dict) -> str:
    """A block listing properties of array items."""
    if not (items := values.get("items")):
        return ""
    if not (item_props := items.get("properties")):
        return ""
    return "\nOptions for each entry include:\n\n" + "\n".join(
        sub_section(k, v) for k, v in item_props.items()
    )


def properties(values: dict) -> str:
    """A block listing object properties."""
    if not (properties := values.get("properties")):
        return ""
    return "\nThis setting has the following sub-options:\n\n" + "\n".join(
        sub_section(k, v) for k, v in properties.items()
    )


def sub_section(prop: str, values: dict) -> str:
    """Formats a bullet point about the given sub-property."""
    sep = lambda: globals()["sep"](values)
    type_str = lambda: globals()["type_str"](values)
    items = lambda: globals()["items"](values)
    properties = lambda: globals()["properties"](values)

    def default() -> str:
        try:
            default = values["default"]
            return f"Defaults to `{json.dumps(default)}`."
        except KeyError:
            return ""

    def description() -> str:
        if not (description := values.get("description")):
            return "MISSING DESCRIPTION\n"

        return f"{description}{p(default(), sep())}\n"

    return (
        f"* `{prop}`{p(type_str())}: "
        + f"{indent(description(), first_line=False)}"
        + indent(items())
        + indent(properties())
    )


def section(prop: str, values: dict) -> str:
    """Formats a section about the given property."""
    sep = lambda: globals()["sep"](values)
    type_str = lambda: globals()["type_str"](values)
    items = lambda: globals()["items"](values)
    properties = lambda: globals()["properties"](values)

    def default_str() -> str:
        try:
            default = values["default"]
        except KeyError:
            t = values.get("type", [])
            if "object" == t or "object" in t:
                # Skip objects as they probably have child defaults.
                return ""
            return "There is no default for this option."

        return f"Defaults to `{json.dumps(default)}`."

    def title() -> str:
        return f"### `{prop}`\n"

    def description() -> str:
        if not (description := values.get("description")):
            return "MISSING DESCRIPTION\n"
        return f"\n{a(em(type_str()))}{description}{p(default_str(), sep())}\n"

    def example_str(example: Any) -> str:
        return "```yaml\n" + f"{yaml.dump({prop: example}, sort_keys=False)}" + "```\n"

    def examples() -> str:
        if not (examples := values.get("examples")):
            return ""

        examples_str = "\n".join(example_str(e) for e in examples)

        if len(examples) >= 2:
            return f"\nExample configurations:\n{examples_str}"
        else:
            return f"\nExample configuration:\n{examples_str}"

    return "---\n" + title() + description() + items() + properties() + examples()


def main() -> None:
    try:
        script_name = "???.py"
        script_name = sys.argv[0]
        schemafile = sys.argv[1]
    except IndexError:
        print("No schema file provided.", file=sys.stderr)
        print(f"Usage: {script_name} <JSON Schema file>", file=sys.stderr)
        print(f"\n{__doc__}", file=sys.stderr)
        exit(1)

    with open(schemafile) as f:
        schema = json.load(f)

    schema = resolve_local_refs(schema)

    sections = (section(k, v) for k, v in schema["properties"].items())
    print(HEADER + "".join(sections), end="")


if __name__ == "__main__":
    main()
