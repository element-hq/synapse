Add 'on_user_search' callback to the third_party_rules section of the [module API](https://matrix-org.github.io/synapse/latest/modules/writing_a_module.html).
This allows to write modules that can trigger on search requests on the user directory and alter the results.
Possible use cases:
- Filter or group user search results
- Exclude MxIDs from the user search results
- Include MxIDs in the user search results even if they do not match the search criteria (e.g. a helper Bot)
Contributed by @awesome-michael.