I'd like to develop a Python script that is able to make a read-only mirror of
the Elm package server and be able to keep it up to date.

The idea is that I'd be able to just run the script and it would download both
the package index from the Elm package server as well as the actual package
content itself (which is currently hosted on GitHub) and put them all on a
single server as a single folder. Then we should be able to put a simple
fileserver in front of these files and serve them up in a way that Zokka (see
https://github.com/Zokka-Dev/zokka-compiler) is able to consume as a read-only
repository. The script will need to modify the package index file to modify the
URLs to point instead at URLs local to the server itself (since on the Elm
package server they point instead to GitHub URLs). In essence I want the file
server to host both the package index and the actual package content where
currently the Elm package server only hosts the package index.

I mentioned that I'd like for the script to be able to keep things up to date
over time as well. So if the script is able to see that there is already a local
package index and set of packages already downloaded, then it should diff that
against the Elm package server and only download packages that have not yet been
downloaded from the package server. The main use case this is supposed to serve
is e.g. a cronjob that runs this script every hour to pick up any packages that
have been uploaded to the Elm package server in the last hour.

You'll need to be careful to make sure that we don't end up in an inconsistent
state if the script crashes or the internet connection goes down while doing
this update. So make sure you're careful about making sure no matter what
happens during the update process, we always have a consistent state that
another run of the script can pick up from with no corruption or loss of
packages in the middle.

The script should have a setting to be able to comprehensively verify the
integrity of the packages that have been pulled donw. E.g. at minimum it should
check that all the file paths set in the package index file actually exist and
that the hashes match the actual content.

I'd like to have an option for the script to only pull down a white-list set of
packages specified as a JSON file. This makes it possible to test the script
without pulling down the entire package repository.

Before starting this, make sure to validate any assumptions I'm making in this
request (e.g. that it is actually possible to make a read-only mirror of the Elm
package server purely with static files and we don't need a dynamic website),
and feel free to ask many follow-up questions to clarify any points of
ambiguity.

Note that I'm also a Zokka developer so if there are changes that need to be
made to Zokka itself, that's also something we can discuss (although I'd prefer
not to).
