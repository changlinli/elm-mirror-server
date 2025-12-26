# Elm Mirror Server

A Python-based read-only mirror of the Elm package server for Elm 0.19 packages.
Use it to:

+ Work offline: Access Elm packages when package.elm-lang.org is unreachable
+ Speed up builds: Run a local mirror for faster CI/team package downloads
+ Archive packages: Preserve Elm packages independently of the official server

This is a single Python 3.10+ script with no other dependencies. Just take the
`elm_mirror.py` file and run `python elm_mirror.py` straight out of the box with
the appropriate flags.

*Note that in order to download and compile packages from this mirror, you'll
need [Zokka](https://github.com/Zokka-Dev/zokka-compiler), an alternative Elm
compiler, to consume packages (the standard Elm compiler has a hardcoded package
server URL).*

Unlike the standard Elm package server which does not actually store packages,
but only stores references to GitHub locations where the packages actually
reside, this mirror downloads all packages locally. This means that if you
choose to mirror all Elm packages, you will need to have a decent amount of
storage available (as of December 2025, all public Elm 0.19 packages together is
about 3.5 GB).

## Quick Start

### 1. Sync packages from the official server

**If you would like to sync from the official server please first run the
`download_preseeded_mirror.sh` script that can be found in this repository.**
E.g. like so

```bash
# This will create a directory named `mirror` on your local machine that is
# pre-seeded with all Elm packages up to Dec 24th, 2025
./download_preseeded_mirror.sh .
```

This script will download all Elm packages as of Dec 24th, 2025 from pre-formed
`.tar.gz` files in this project's GitHub releases. This reduces the load on the
main Elm package server and only hits the Elm package server (and other GitHub
links) for packages since that date. Note that the download may take a while
since, as stated previously, it'll be about 3.5 GB to download. (N.B. if you're
wondering why the `tar` files are gzipped if Elm package source code is already
zipfiles, turns out there's actually a noticeable amount of compression that
still happens!)

```bash
# Set up GitHub auth (increases rate limit from 60 to 5000 requests/hour)
# Not necessary if you're only planning to sync a small subset of packages
# Read on to see how to do that
export GITHUB_TOKEN=your_token_here

# Sync all packages to ./mirror
python elm_mirror.py sync --mirror-content ./mirror --incremental-sync
```

Note that as long as you have pre-seeded `mirror`, even if you didn't run
`--incremental-sync`, the load wouldn't be awful on the Elm server. You would
hit the `all-packages` endpoint which serves up a JSON object that is, as of Dec
2025, about 216 KB (and compressed so that it is 47 KB over the wire), instead
of the `since` endpoint (which likely is less than ~10 KB). The biggest
difference is making sure that `mirror` is pre-seeded. Otherwise every Elm
package will require a trip to the Elm server to download its package-specific
metadata.

### 2. Serve the mirror

If you don't care about keeping your mirror up to date with new Elm packages,
you can run.

```bash
python elm_mirror.py serve \
    --mirror-content ./mirror \
    --port 8000 \
    --base-url http://localhost:8000 
```

Note that you should substitute `http://localhost:8000` with whatever URL you
expect packages to be available at, because this is the URL prefix that we will
tell an Elm compiler where to fetch a package from. For example, if you are
making this mirror publicly available at `https://example.com` behind a reverse
proxy, even though you can hit it locally at `localhost` from the server, the
`--base-url` should be set to `https://example.com` because that is where
clients will expect to find packages.

If you would like to make sure that the mirror states up to date with the
latest Elm packages, you can pass additional arguments to make sure it syncs
with the main package server in the background. If you do so we would highly
recommend you use `--incremental-sync` to reduce load on the main package
repository. 

Note that adding `--sync-interval` causes `serve` to subsume `sync`'s function
(the first thing `serve` will do is perform a `sync`).

```bash
python elm_mirror.py serve \
    --mirror-content ./mirror \
    --port 8000 \
    --base-url http://localhost:8000 \
    --sync-interval 86400 \ # Sync every day
    --incremental-sync
```

### 3. Configure Zokka to use the mirror

Zokka is a drop-in compatible variant of the vanilla Elm compiler that supports custom package repositories:

```bash
export ELM_HOME="elm_home"  # or ~/.elm for global config
mkdir -p "$ELM_HOME/0.19.1/zokka/"
cat > "$ELM_HOME/0.19.1/zokka/custom-package-repository-config.json" << EOF
{
    "repositories": [
        {
            "repository-type": "package-server-with-standard-elm-v0.19-package-server-api",
            "repository-url": "http://localhost:8000",
            "repository-local-name": "local-mirror"
        }
    ],
    "single-package-locations": []
}
EOF

# Use zokka exactly like you would use elm
npx zokka make src/Main.elm
```

## Commands

| Command | Description |
|---------|-------------|
| `python elm_mirror.py sync` | Download packages from package.elm-lang.org |
| `python elm_mirror.py serve` | Run HTTP server to serve mirrored packages |
| `python elm_mirror.py verify` | Check integrity of downloaded packages |

## Common Options

**For `sync`:**
- `--mirror-content DIR`: Where to store packages (default: `.`)
- `--incremental-sync`: Only fetch new packages since last sync
- `--package-list FILE`: JSON file to sync specific packages only

**For `serve`:**
- `--base-url URL`: Public URL for the mirror (required)
- `--port PORT`: Port to listen on (default: 8000)
- `--sync-interval SECS`: Enable background sync at this interval

## Selective Sync

To sync only specific packages, create a JSON file:

```json
["elm/core", "elm/html", "mdgriffith/elm-ui@2.0.0"]
```

Then run:
```bash
python elm_mirror.py sync --package-list packages.json --mirror-content ./mirror
```

## GitHub Token

Without a token, GitHub limits you to 60 requests/hour. With a token, you get
5000/hour. By default, we throttle to 4000 total HTTP requests per hour across
all endpoints. Hence if you don't use a GitHub token, you may quickly run into
GitHub rate limiting issues.

Create a token at: **GitHub → Settings → Developer settings → Personal access tokens**

Note that your token doesn't need *any special permissions*. It just needs to be
able to read public repositories (which is the minimal set of permissions any
token comes with). All GitHub cares about is that the token is associated with
your account.

Once you have the token, make sure it is available as an environment variable
under `GITHUB_TOKEN` and all the commands will automatically pick it up.
