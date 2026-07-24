# what is this
this is the website for the hack club [atlantis](https://atlantis.hacklub.com) YSWS! 

## dev
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

CLOUDFLARE_TOKEN = CLOUDFLARE_TOKEN_GOES_HERE
R2_ACCESS_KEY_ID = R2_ACCESS_KEY_ID_GOES_HERE
R2_ACCESS_KEY = R2_ACCESS_KEY_GOES_HERE
R2_ENDPOINT = https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
R2_BUCKET_NAME = R2_BUCKET_NAME_GOES_HERE

PRINTABLES_GRAPHQL_URL = https://api.printables.com/graphql/                     
```

### hack club auth
this is for the `HCA_CLIENT_ID`, `HCA_CLIENT_SECRET`, and `HCA_CALLBACK_URI` fields! the process for getting these is quite simple, just:
- head over to [hack club auth](https://identity.hackclub.com)
- enable developer mode
- create a new app, call it whatever you want, and grab the client id and client secret
- change the redirect uri to `http://localhost:8000/oauth/callback`
...and that's it for auth!

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

### cloudflare stuff
while the R2 bucket that's used for object storage is free, to obtain one you **need** to provide them with a valid credit/debit card!
- get a cloudflare R2 bucket (i'm not explaining how to do this, google it! it's not too difficult but you **will** need a payment method.)
- head to your R2 dash
- click on "API tokens"
- create an account api token with access to your bucket
- get your token (`CLOUDFLARE_TOKEN`), access key id, access key, and the S3 endpoint url (`R2_ENDPOINT`)
- also get your bucket name and paste that in

the bucket stays **private** — you do **not** need to enable a public development URL. uploaded files are served back to the browser through the app's `serve_media` proxy view, which streams them from R2 using your S3 credentials.

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