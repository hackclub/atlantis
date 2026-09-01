# what is this
this is the website for the hack club [atlantis](https://atlantis.hackclub.com) YSWS! 

## dev

### this is hella outdated, i'll get around to updating it eventually
so... you want to help develop the website? here's some detailed setup instructions

- run `git clone https://github.com/hellonearth311/atlantis.git`
- create a .env file with this template
```
HCA_CLIENT_ID = CLIENT_ID_GOES_HERE
HCA_CLIENT_SECRET = CLIENT_SECRET_GOES_HERE
HCA_CALLBACK_URI = http://localhost:8000/oauth/callback

SECRET_KEY=dev-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
ALLOW_JOURNALING = True

POSTGRES_DB = django
POSTGRES_USER = django
POSTGRES_PASSWORD = django
POSTGRES_HOST = localhost
POSTGRES_PORT = 5432

SLACK_TOKEN = SLACK_TOKEN_GOES_HERE
DEFAULT_PFP = https://cdn.hackclub.com/019ee160-b8f6-7920-aca0-6e35fffc2b6a/slack_hash_256.png
REVIEW_CHECKPOINT_ID = REVIEW_CHECKPOINT_CHANNEL_ID_GOES_HERE

CLOUDFLARE_TOKEN = CLOUDFLARE_TOKEN_GOES_HERE
R2_ACCESS_KEY_ID = R2_ACCESS_KEY_ID_GOES_HERE
R2_ACCESS_KEY = R2_ACCESS_KEY_GOES_HERE
R2_ENDPOINT = https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
R2_BUCKET_NAME = R2_BUCKET_NAME_GOES_HERE

PRINTABLES_GRAPHQL_URL = https://api.printables.com/graphql/                     

AIRTABLE_PAT = AIRTABLE_PERSONAL_ACCESS_TOKEN_GOES_HERE
AIRTABLE_BASE_ID = appXXXXXXXXXXXXXX
AIRTABLE_TABLE_ID = tblXXXXXXXXXXXXXX

LAPSE_CLIENT_ID = LAPSE_OAUTH_CLIENT_ID_GOES_HERE
LAPSE_REDIRECT_URI = https://atlantis.hackclub.com/projects/
```

### lapse

time is never typed in. a shipper records their CAD on [lapse](https://lapse.hackclub.com),
publishes there, and the picker on the book reads their published timelapses back over the
[lapse api](https://api.lapse.hackclub.com/docs) so they can tape one into a lapse.

`LAPSE_CLIENT_ID` is an oauth2 app on lapse with the `timelapse:read`, `snapshot:read` and
`user:read` scopes. it's a **public** client — authorization is pkce (`S256`), so there is
no client secret to configure and nothing secret goes in the browser but a hash.

`LAPSE_REDIRECT_URI` has to match what's registered on lapse *exactly*, on both the
authorize and the token call. it's registered as the projects list, so there is no callback
route of its own: `projects` picks the code up when it sees one, and the book the shipper
started from is remembered in their session rather than round-tripped through lapse. for
local development, register a second app whose redirect uri is `http://localhost:8000/projects/`
and point `LAPSE_REDIRECT_URI` at it.

two things about the api are worth knowing before touching `lapse.py`:

- **a failed call still comes back HTTP 200**, with `{"ok": false, "error": ..., "message": ...}`.
  the envelope is what says whether a call worked, so that's what's checked.
- **a timelapse's `duration` is *recorded* seconds, not the length of the video.** the
  compiled video runs sixty times faster — the api reports `720` for a video that is twelve
  seconds long — which is the same ratio the review desks already read footage in
  (`TRACKED_SECONDS_PER_VIDEO_SECOND`). so `duration` lands on `tracked_seconds` unchanged
  and the video timeline is derived from it. reading it as a video length would multiply
  every shipper's hours by sixty.

there's no refresh grant on the token endpoint — `/auth/token` accepts `authorization_code`
and nothing else — so an expired token can't be renewed behind the shipper's back. the
picker notices and asks them to reconnect.

the durations the picker draws are for the shipper's benefit only. when a lapse is actually
written, `create_journal` re-reads the footage from the api and takes the tracked time off
*that*: the form only ever sends a list of ids, because tracked time is what turns into
hours and then into money.

### hack club auth
this is for the `HCA_CLIENT_ID`, `HCA_CLIENT_SECRET`, and `HCA_CALLBACK_URI` fields! the process for getting these is quite simple, just:
- head over to [hack club auth](https://identity.hackclub.com)
- enable developer mode
- create a new app, call it whatever you want, and grab the client id and client secret
- change the redirect uri to `http://localhost:8000/oauth/callback`
...and that's it for auth!

one gotcha: the app asks for the `birthdate` scope (it's a scope of its own on HCA, not
part of `profile`), because the Airtable submission needs a birthday. tokens issued before
that scope was added don't carry the claim, so those users submit without a birthday until
the next time they log in.

the `verification_status` scope carries two claims, not one: `verification_status`
(`needs_submission` / `pending` / `verified` / `ineligible`) and `ysws_eligible` (a
boolean, or absent while HCA has no verdict). both are stored on the profile at login,
and creating a project or shipping is gated on them — HCA decides who is eligible for
YSWS prizes, not us. without that scope nobody can create or ship, since a missing claim
is not a yes. a blocked user's state is re-fetched from HCA at most once a minute, so an
approval that lands mid-session takes effect without a fresh login.

### second section
i didn't know what to call this one, but all these values are fine. i'll still explain what they do because i'm kind.
- `SECRET_KEY`: something django needs to work, in prod it has to be generated with a terminal command but in dev this is fine
- `DEBUG`: django debug mode, easier debugging w/ tracebacks. keep on unless testing production behavior.
- `ALLOWED_HOSTS`: allowed django hosts to run on.
- `ALLOW_JOURNALING`: allow users without the organizer permission to journal

### postgres
all of this stuff also stays the same lol

### slack stuff
leave `DEFAULT_PFP` the same. to get your `SLACK_TOKEN`:
- create an app on [slack](https://api.slack.com/apps/) 
- go to "OAuth and Permissions"
- go to bot token scopes and allow `users.profile:read` and `users.read`
- install the app to hack club
- get your `xoxb-` token!

`REVIEW_CHECKPOINT_ID` is the channel id (the `C...` one, from the channel's "view details" pane) that every T1/T2 review gets posted in. the shipper and the reviewer are both pinged there with the reviewer's feedback, so a decision the shipper wants to argue with always lands somewhere they can reply — no T1/T2 outcome is DM'd. T3 is the exception: a finalization or a return from fraud review is DM'd to the shipper and posted nowhere. the bot needs `chat:write` and has to be in that channel; leave it blank and T1/T2 reviews just don't get announced at all.

### cloudflare stuff
while the R2 bucket that's used for object storage is free, to obtain one you **need** to provide them with a valid credit/debit card!
- get a cloudflare R2 bucket (i'm not explaining how to do this, google it! it's not too difficult but you **will** need a payment method.)
- head to your R2 dash
- click on "API tokens"
- create an account api token with access to your bucket
- get your token (`CLOUDFLARE_TOKEN`), access key id, access key, and the S3 endpoint url (`R2_ENDPOINT`)
- also get your bucket name and paste that in

the bucket stays **private** — you do **not** need to enable a public development URL. uploaded files are served back to the browser through the app's `serve_media` proxy view, which streams them from R2 using your S3 credentials.

### airtable
when a T3 reviewer approves a ship, the project is submitted to the **YSWS Project
Submission** table as one record — that record is what pays the shipper, so this is the
last step of the pipeline.

- make a [personal access token](https://airtable.com/create/tokens) with the
  `data.records:write` scope on the base, and paste it in as `AIRTABLE_PAT`
- `AIRTABLE_BASE_ID` is the `app...` in the base's URL, `AIRTABLE_TABLE_ID` the `tbl...`
- optional: `AIRTABLE_API_BASE_URL` (default `https://api.airtable.com/v0`) and
  `AIRTABLE_URL_EXPIRE_SECONDS` (default 7 days — how long the presigned R2 links for the
  screenshot and the editor model stay valid)

leave the credentials blank and finalization still works: the ship is finalized, the payout
happens, and the submission is recorded as failed with the missing settings named. nothing
is ever sent to the browser — the token only leaves the server in an `Authorization` header.

`Optional - Override Hours Spent Justification` is the field HQ reads as the unified
justification, so it carries the whole audit trail: the T2 reviewer's justification
verbatim, then every timelapse on the ship — what the timelapse reviewer said each one
showed, the ranges they cut out of it, and the reason given for each cut. T3 reviewers see
exactly that text on the fraud review page before they approve.

submissions are once-per-ship — the `AirtableSubmission` row is claimed in the database
before the request goes out, so a retried finalization cannot make a second record. one
that failed (Airtable down, token not yet granted write access) is picked up again by:

```
python manage.py submit_airtable          # --dry-run to see what it would send
```

a submission whose request went out but never came back is *not* retried automatically —
that's the one case where retrying would duplicate a record. the command names those so
somebody can check the table by hand, and `/admin` lets an organizer resolve one by pasting
the record id in or setting the status back to failed.

### inactivity detection (ffmpeg)

the timelapse review page draws a second track under each recording's timeline showing the
stretches where nothing on screen changed — the screen somebody walked away from, the
tutorial left playing, the half hour of an idle editor. it's advisory: it never removes
time by itself, it only says where to look, and every deduction is still a range a
reviewer drew with a reason attached.

it's one ffmpeg pass per video (sample at 1fps, subtract consecutive frames, ask
`blackframe` which of the differences are black), so **ffmpeg has to be on PATH** — on
macOS `brew install ffmpeg`, on debian `apt install ffmpeg`. the docker image installs it,
so a containerised deploy needs nothing extra.

**it runs itself when somebody creates a journal.** the entry's timelapses are analysed on a
worker thread the moment the attachment commits — a pass is minutes of ffmpeg and nobody's
browser waits for it, so the request returns straight away and the footage is usually
already analysed by the time a reviewer opens it. at most `MAX_CONCURRENT_CHECKS`
(`activity.py`) videos are in flight at once, so a rush of journals queues instead of
forking an ffmpeg per submission.

the command is the catch-up for everything that hook missed — footage from before it
existed, a video the site couldn't reach, a worker restarted mid-pass:

```
python manage.py check_timelapse_activity            # everything not yet analysed
python manage.py check_timelapse_activity --limit 20
python manage.py check_timelapse_activity --project 7 --force
```

the same pass also records how long the video actually runs, which is what the review
page draws its timeline against — before anything has measured one, the length is
estimated from the session's screenshot count (one shot is one second of footage), so
`--force` over already-analysed sessions is worth a run to replace those estimates.

a session whose video couldn't be fetched or read is left *unchecked* rather than recorded
as clean, so the next run picks it up again — the review page draws "not analysed" and
"no inactivity found" differently on purpose, because they aren't the same claim. nothing
in the review is blocked by a session that has never been analysed, and that's what makes
the background hook safe to lose: a check that never finished is a session still waiting
for the command, not one recorded as clean.

### launching server/docker
- run `python -m venv .venv`
- wait for vscode to detect the venv and activate it
- run `pip install -r requirements.txt`
- run `cd atlantis`
- run `python manage.py migrate`
- run `docker compose up db`
- open a new terminal tab and run `python manage.py runserver`
- head to `localhost:8000` and enjoy

if you need any help with any of this setup, feel free to DM @swn on the Hack Club Slack or email me at `swarit@shipwrights.dev`
