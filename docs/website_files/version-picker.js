
const dropdown = document.querySelector('.version-picker .dropdown');
const dropdownMenu = dropdown.querySelector('.dropdown-menu');

fetchVersions(dropdown, dropdownMenu).then(() => {
    initializeVersionDropdown(dropdown, dropdownMenu);
});

/**
 * Initialize the dropdown functionality for version selection.
 * 
 * @param {Element} dropdown - The dropdown element.
 * @param {Element} dropdownMenu - The dropdown menu element.
 */
function initializeVersionDropdown(dropdown, dropdownMenu) {
    // Toggle the dropdown menu on click
    dropdown.addEventListener('click', function () {
        this.setAttribute('tabindex', 1);
        this.classList.toggle('active');
        dropdownMenu.style.display = (dropdownMenu.style.display === 'block') ? 'none' : 'block';
    });

    // Remove the 'active' class and hide the dropdown menu on focusout
    dropdown.addEventListener('focusout', function () {
        this.classList.remove('active');
        dropdownMenu.style.display = 'none';
    });

    // Handle item selection within the dropdown menu
    const dropdownMenuItems = dropdownMenu.querySelectorAll('li');
    dropdownMenuItems.forEach(function (item) {
        item.addEventListener('click', function () {
            dropdownMenuItems.forEach(function (item) {
                item.classList.remove('active');
            });
            this.classList.add('active');
            dropdown.querySelector('span').textContent = this.textContent;
            dropdown.querySelector('input').value = this.getAttribute('id');

            window.location.href = changeVersion(window.location.href, this.textContent);
        });
    });
};

/**
 * The version list comes from the GitHub API, which answers with
 * `Cache-Control: public, max-age=60` and an ETag. We used to pass
 * `cache: "force-cache"`, which makes the browser reuse a cached response no
 * matter how old it is, so browsers kept the old list until a hard
 * refresh. With the default cache mode every browser follows GitHub's headers
 * instead: reuse the response for 60 seconds, then revalidate it with the ETag.
 *
 * That alone is not enough: unauthenticated requests are limited to 60 per hour
 * per IP, and a 304 from revalidation still counts against the limit
 * (only authenticated 304s are free). Someone clicking through chapters could use
 * it up, and once rate limited the menu would be empty. So we keep the list
 * in localStorage, skip the request if the last fetch was less than
 * VERSIONS_REFRESH_INTERVAL_MS ago, and fall back to the stored list if the
 * request fails.
 *
 * This only concerns the version list. The doc pages are served by GitHub
 * Pages with `Cache-Control: max-age=600` and an ETag, so browsers already
 * reuse cached pages and only revalidate them (a 304 with no body when
 * unchanged). We can't set headers on GitHub Pages, and nothing here bypasses
 * that cache.
 */
const VERSIONS_STORAGE_KEY = "synapse-docs-versions";
const VERSIONS_REFRESH_INTERVAL_MS = 10 * 60 * 1000; // 10 minutes

/**
 * This function fetches the available versions from a GitHub repository
 * and inserts them into the version picker.
 * 
 * @param {Element} dropdown - The dropdown element.
 * @param {Element} dropdownMenu - The dropdown menu element.
 * @returns {Promise<Array<string>>} A promise that resolves with an array of available versions.
 */
function fetchVersions(dropdown, dropdownMenu) {
    return new Promise((resolve, reject) => {
        window.addEventListener("load", () => {
            const stored = readStoredVersions();
            const storedAge = stored ? Date.now() - stored.fetchedAt : Infinity;
            const storedIsRecent =
                storedAge >= 0 && storedAge < VERSIONS_REFRESH_INTERVAL_MS;

            const versionsPromise = storedIsRecent
                ? Promise.resolve(stored.versions)
                : fetch("https://api.github.com/repos/element-hq/synapse/git/trees/gh-pages")
                    .then((res) => {
                        if (!res.ok) {
                            throw new Error("GitHub API returned " + res.status);
                        }
                        return res.json();
                    })
                    .then((resObject) => {
                        const excluded = ['dev-docs', 'v1.91.0', 'v1.80.0', 'v1.69.0'];
                        const tree = resObject.tree.filter((item) => item.type === "tree" && !excluded.includes(item.path));
                        const versions = tree.map((item) => item.path);
                        storeVersions(versions);
                        return versions;
                    })
                    .catch((ex) => {
                        if (!stored) throw ex;
                        console.warn("Failed to fetch version data, using the stored list", ex);
                        return stored.versions;
                    });

            versionsPromise.then(storedOrFetched => {
                const versions = storedOrFetched.slice().sort(sortVersions);
                // Create a list of <li> items for versions
                versions.forEach((version) => {
                    const li = document.createElement("li");
                    li.textContent = version;
                    li.id = version;

                    if (window.SYNAPSE_VERSION === version) {
                        li.classList.add('active');
                        dropdown.querySelector('span').textContent = version;
                        dropdown.querySelector('input').value = version;
                    }

                    dropdownMenu.appendChild(li);
                });

                resolve(versions);

            }).catch(ex => {
                console.error("Failed to fetch version data", ex);
                reject(ex);
            })
        });
    });
}

function readStoredVersions() {
    try {
        const stored = JSON.parse(localStorage.getItem(VERSIONS_STORAGE_KEY));
        if (stored && Array.isArray(stored.versions) && typeof stored.fetchedAt === "number") {
            return stored;
        }
    } catch (ex) {
        // localStorage can be unavailable (e.g. storage blocked by the user)
        // or hold invalid JSON. Then we simply fetch on every page load.
    }
    return null;
}

function storeVersions(versions) {
    try {
        localStorage.setItem(VERSIONS_STORAGE_KEY, JSON.stringify({ fetchedAt: Date.now(), versions: versions }));
    } catch (ex) {
        // Same as above: storage unavailable or full, nothing to do.
    }
}

/**
 * Custom sorting function to sort an array of version strings.
 *
 * @param {string} a - The first version string to compare.
 * @param {string} b - The second version string to compare.
 * @returns {number} - A negative number if a should come before b, a positive number if b should come before a, or 0 if they are equal.
 */
function sortVersions(a, b) {
    // Put 'develop' and 'latest' at the top
    if (a === 'develop' || a === 'latest') return -1;
    if (b === 'develop' || b === 'latest') return 1;

    // If any of the versions do not confrom to a semantic version string, they
    // will be sorted behind a valid version.
    const versionA = (a.match(/v(\d+(\.\d+)+)/) || [])[1]?.split('.') ?? '';
    const versionB = (b.match(/v(\d+(\.\d+)+)/) || [])[1]?.split('.') ?? '';

    for (let i = 0; i < Math.max(versionA.length, versionB.length); i++) {
        if (versionB[i] === undefined) {
            return -1;
        }
        if (versionA[i] === undefined) {
            return 1;
        }

        const partA = parseInt(versionA[i], 10);
        const partB = parseInt(versionB[i], 10);

        if (partA > partB) {
            return -1;
        } else if (partB > partA) {
            return 1;
        }
    }

    return 0;
}

/**
 * Change the version in a URL path.
 *
 * @param {string} url - The original URL to be modified.
 * @param {string} newVersion - The new version to replace the existing version in the URL.
 * @returns {string} The updated URL with the new version.
 */
function changeVersion(url, newVersion) {
    const parsedURL = new URL(url);
    const pathSegments = parsedURL.pathname.split('/');

    // Modify the version
    pathSegments[2] = newVersion;

    // Reconstruct the URL
    parsedURL.pathname = pathSegments.join('/');

    return parsedURL.href;
}
