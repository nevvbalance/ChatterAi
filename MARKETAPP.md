# Marketapp API integration

The bot now has a minimal Marketapp API integration.

## Authentication

Marketapp requires the API token in the `Authorization` header **without** a `Bearer` prefix.

The bot reads the token from the environment variable:

`MARKETAPP_API_TOKEN`

## Test command

In Telegram, send:

`/marketapp`

The bot calls:

`GET https://api.marketapp.org/v1/collections/`

and returns a compact summary of the response.

## Deployment

Add this environment variable to the service that runs the bot:

- `MARKETAPP_API_TOKEN` = your Marketapp API token

Do not put the real token into GitHub source files, README files, or commits.

The existing Telegram bot still uses `BOT_TOKEN` and `RENDER_EXTERNAL_URL` as before.

## API notes

The current Marketapp OpenAPI documentation exposes collections, NFTs, gifts, Fragment operations, and rental operations. Some older collections/gifts/NFT endpoints are marked deprecated in the documentation. Authentication is API-token based.
