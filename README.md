# what is this
this is the website for the hack club [atlantis](https://atlantis.hackclub.com) YSWS! 

note to reviewers: head to ```/login-test/``` on the site to test it out, as ```/``` is now the landing page! (this will be changed upon launch)

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

LOOKOUT_TOKEN = LOOKOUT_API_KEY_GOES_HERE
```

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

`REVIEW_CHECKPOINT_ID` is the channel id (the `C...` one, from the channel's "view details" pane) that rejected T1/T2 reviews get posted in. the shipper and the reviewer are both pinged there with the reviewer's feedback, instead of the shipper getting a DM they can't reply to. approvals and finalizations are still DM'd. the bot needs `chat:write` and has to be in that channel; leave it blank and rejections just don't get announced.

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
verbatim, then every Lookout on the ship, the ranges timelapse review cut out of each one,
and the reason given for each cut. T3 reviewers see exactly that text on the fraud review
page before they approve.

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
