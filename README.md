# urirun-connector-bitbucket

Apache-2.0 URI process connector for governed Bitbucket Cloud repository,
pull-request, branch archive, deletion, and restoration operations.

Authentication is resolved indirectly from `BITBUCKET_TOKEN_REF` (default
`getv://BITBUCKET_TOKEN`). Mutations require an exact expected commit and an
idempotency key, and return a normalized forge operation receipt after readback.
