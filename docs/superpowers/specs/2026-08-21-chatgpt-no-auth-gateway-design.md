/Users/yosribahri/.zlogin:9: nice(5) failed: operation not permitted
# ChatGPT No-Auth Gateway Design

## Goal

Allow a ChatGPT Web custom connector configured as **No Auth** to use one
server-owned Plane personal access token (PAT) without sending that PAT to
ChatGPT or exposing an unauthenticated, predictable MCP endpoint.

The existing OAuth and caller-supplied PAT transports remain unchanged.

## Security model

ChatGPT Web's No Auth mode cannot add an authorization header. Consequently,
the only deployable authentication factor available to this connector is an
unguessable capability embedded in its endpoint URL.

The gateway is disabled unless all three variables are present:

- `MCP_GATEWAY_TOKEN`: an independently generated random capability of at
  least 32 characters;
- `PLANE_API_KEY`: the server-owned Plane PAT;
- `PLANE_WORKSPACE_SLUG`: the single workspace exposed by the gateway.

When enabled, the MCP endpoint is:

```text
https://<host>/<optional-prefix>/http/chatgpt/<MCP_GATEWAY_TOKEN>/mcp
```

The Plane PAT and workspace slug are read only by the server and cannot be
overridden by connector headers. The Plane PAT and gateway capability never
occur in MCP responses or application logs. The workspace slug is Plane routing
metadata, not an authentication secret: valid Plane resource data returned by a
tool may naturally contain it. Operators that require workspace-name
confidentiality must use OAuth and/or an output policy. The gateway capability
can be rotated without rotating the Plane PAT.

The capability URL must be treated as a credential. Deployments must use TLS,
must not place it in source control, and must configure reverse-proxy access
logs to redact or omit this route. Anyone possessing the URL has the gateway's
Plane permissions, so OAuth remains the preferred mode for per-user identity,
consent, and revocation.

## Architecture

Add a fourth FastMCP factory, `get_gateway_mcp`, with no FastMCP auth provider.
It uses the existing common middleware and tool registration. During calls,
`get_plane_client_context` already falls back to `PLANE_API_KEY` and
`PLANE_WORKSPACE_SLUG` when no request access token exists, so no PAT injection
into inbound HTTP headers is needed.

The HTTP entry point validates gateway configuration before mounting the app.
If `MCP_GATEWAY_TOKEN` is absent, no gateway route is registered. If it is not
at least 32 characters from the literal URL-safe capability alphabet
`A-Z`, `a-z`, `0-9`, `_`, and `-`, or either Plane credential is missing,
startup fails with a configuration error rather than exposing a broken, weak,
or route-template-injectable endpoint.

When valid, Starlette mounts the gateway app beneath the exact capability path.
Requests using any other token do not match a route. The gateway participates
in the combined lifespan only when enabled.

The startup log announces that the gateway is enabled but never includes the
token, full route, PAT, or fixed workspace metadata. Normal stdio logs retain
their configured workspace metadata, and authenticated HTTP logs retain
workspace metadata derived from the authenticated access token.

## Configuration and deployment

README documentation will include:

1. generation of a high-entropy gateway token;
2. Docker/secret environment configuration for the three required values;
3. the ChatGPT Web connector URL and the **No Auth** selection;
4. a Traefik example that forwards the route without injecting the Plane PAT;
5. TLS, access-log redaction, least-privilege PAT, rotation, and endpoint
   non-sharing requirements;
6. an explicit recommendation to use the existing OAuth endpoint where
   possible.

Environment variables are preferred over proxy-side credential injection
because the application can validate configuration, tests can cover the whole
flow, and the Plane PAT does not need to cross an internal HTTP hop. Container
or orchestrator secret stores should supply the environment variables in
production.

## Error handling

- No `MCP_GATEWAY_TOKEN`: gateway disabled, existing HTTP transports start.
- Token shorter than 32 characters or containing a character outside
  `[A-Za-z0-9_-]`: startup error naming only the variable.
- Gateway enabled without either Plane credential: startup error naming only
  the missing variable.
- Wrong capability URL: ordinary 404 response with no information about the
  configured token.
- Plane rejects or cannot use the server PAT: existing Plane client/tool error
  behavior applies without echoing the PAT.

## Testing

Tests will be written before production changes and will cover:

- configuration validation for missing and weak values;
- gateway disabled by default;
- exact routing for the correct and incorrect capability path;
- rejection of route-template and other non-URL-safe capability values;
- successful MCP initialization through the correct path without request auth
  headers;
- a real gateway tool call proving hostile authorization/workspace headers
  cannot override the server-side PAT and workspace;
- preservation of the existing OAuth and header-auth routes;
- absence of gateway and Plane secrets from production-formatted startup logs
  and HTTP responses, including noncanonical trailing-slash requests.

After implementation, the complete test suite, formatter/linter, and package
build will be run. Live Plane integration tests will be run only if dedicated
test credentials are available; otherwise this limitation will be reported.

## Non-goals

- Per-user authorization, audit identity, or Plane permissions in No Auth mode.
- Replacing the existing OAuth or PAT-header endpoints.
- Accepting the Plane PAT in a URL or query parameter.
- Implementing IP allowlisting, because ChatGPT egress addresses are not a
  stable connector authentication mechanism.
