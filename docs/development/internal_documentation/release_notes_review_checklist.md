# Release notes review checklist

The Synapse release process includes a step to review the changelog before
publishing it. The following is a list of common points to check for:

1. Check whether any similar entries that can be merged together (make sure to include all mentioned PRs at the end of the line, i.e. (#1234, #1235, ...)).
2. Link any MSCXXXX lines to the Matrix Spec Change itself: <https://github.com/matrix-org/matrix-spec-proposals/pull/xxxx>.
3. Wrap any class names, variable names, etc. in back-ticks, if needed.
4. Hoist any relevant security, deprecation, etc. announcements to the top of this version's changelog for visibility. This includes any announcements in RCs for this release.
5. Check the upgrade notes for any important announcements, and link to them from the changelog if warranted.
6. Quickly skim and check that each entry is in the appropriate section.
7. Entries under the Bugfixes section should ideally state what Synapse version the bug was introduced in. For example: "Fixed a bug introduced in v1.x.y" or if no version can be identified, "Fixed a long-standing bug ...".