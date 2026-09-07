# what is this
this is the website for the hack club [atlantis](https://atlantis.hackclub.com) YSWS! 

ship 5 hours of CAD a week for 8 weeks, get a free 3D printer!

# timelapses
time is never self-reported. every hour on a lapse comes from a timelapse
attached to it, and there are two sources:

- **[lapse](https://lapse.hackclub.com)** — how it works now. you record and
  publish on lapse, connect your account here once, and the picker on your
  project reads back what you published so you can tape it in.
- **lookout** — legacy. this site used to record your screen itself. it still
  works and everything recorded on it still attaches and reviews the same way,
  but it lives behind the "lookout" tab in the picker rather than the front page.

both land in one `Timelapse` table with a `source` column, so journals, the
internal timelapse review and the airtable audit treat them identically.

## config
| var | what it's for |
| --- | --- |
| `LAPSE_CLIENT_ID` | the oauth client registered with lapse. without it the book says lapse isn't set up rather than offering a connection. |
| `LAPSE_REDIRECT_URI` | where lapse sends the browser back. must match between the authorize and token calls, so set it per deployment (`https://localhost:8000/lapse/callback/` in dev). |
| `LAPSE_API_BASE_URL` / `LAPSE_WEB_BASE_URL` | the api and the site. defaults are the real ones. |
| `LOOKOUT_TOKEN` | legacy, optional. only needed to start *new* lookout recordings. |
| `LOOKOUT_ALLOW_NEW` | set to `False` to retire the old recorder. resuming, attaching and reviewing lookout footage keep working either way. |

the authorization is oauth2 + pkce and there is no refresh grant, so an expired
token means sending the shipper back through the authorize page — the picker
says "reconnect" rather than failing a call.
