# what is this
this is the website for the hack club layered YSWS!

## dev
so... you want to help develop the website? well, you must know django, as that is what this site is written in!
steps:
- create a `.env` file in the `layered` directory
- go to identity.hackclub.com
- create an app
- give it all scopes (scary i know)
- grab the client ID and secret, and set the redirect URI to `http://localhost:8000/oauth/callback`
- go back to `layered/.env` and follow this template:
```
HCA_CLIENT_ID = CLIENT_ID_GOES_HERE
HCA_CLIENT_SECRET = CLIENT_SECRET_GOES_HERE
HCA_CALLBACK_URI = http://localhost:8000/oauth/callback 
```
- that's it! now just make a `.venv` in the root directory, install all deps with `pip install -r requirements.txt`, and enjoy!