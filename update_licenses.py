def main():
    lines = open("licenses").readlines()

    for line in lines:
        filename, notice = line.split(":", 1)

        pl = open(filename).readlines()

        for line_number, line in enumerate(pl):
            if line.startswith("# Copyright"):
                break
        else:
            raise Exception(f"Could not find copyright line in {filename}")

        pl.insert(line_number, notice)

        open(filename, "w").writelines(pl)


if __name__ == "__main__":
    main()
