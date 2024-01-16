### Pull Request Checklist

<!-- Please read https://element-hq.github.io/synapse/latest/development/contributing_guide.html before submitting your pull request -->

* [ ] Pull request is based on the develop branch
* [ ] Pull request includes a [changelog file](https://element-hq.github.io/synapse/latest/development/contributing_guide.html#changelog). The entry should:
  - Be a short description of your change which makes sense to users. "Fixed a bug that prevented receiving messages from other servers." instead of "Moved X method from `EventStore` to `EventWorkerStore`.".
  - Use markdown where necessary, mostly for `code blocks`.
  - End with either a period (.) or an exclamation mark (!).
  - Start with a capital letter.
  - Feel free to credit yourself, by adding a sentence "Contributed by @github_username." or "Contributed by [Your Name]." to the end of the entry.
* [ ] [Code style](https://element-hq.github.io/synapse/latest/code_style.html) is correct
  (run the [linters](https://element-hq.github.io/synapse/latest/development/contributing_guide.html#run-the-linters))
