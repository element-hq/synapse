# Security

This page lays out security best-practices when running Synapse.

If you believe you have encountered a security issue, see our [Security
Disclosure Policy](https://element.io/en/security/security-disclosure-policy).

## Content repository

Matrix serves raw, user-supplied data in some APIs — specifically the [content
repository endpoints](https://matrix.org/docs/spec/client_server/latest.html#get-matrix-media-r0-download-servername-mediaid).

Whilst we make a reasonable effort to mitigate against XSS attacks (for
instance, by using [CSP](https://github.com/matrix-org/synapse/pull/1021)), a
Matrix homeserver should not be hosted on a domain hosting other web
applications. This especially applies to sharing the domain with Matrix web
clients and other sensitive applications like webmail. See
https://developer.github.com/changes/2014-04-25-user-content-security for more
information.

Ideally, the homeserver should not simply be on a different subdomain, but on a
completely different [registered
domain](https://tools.ietf.org/html/draft-ietf-httpbis-rfc6265bis-03#section-2.3)
(also known as top-level site or eTLD+1). This is because [some
attacks](https://en.wikipedia.org/wiki/Session_fixation#Attacks_using_cross-subdomain_cookie)
are still possible as long as the two applications share the same registered
domain.


To illustrate this with an example, if your Element Web or other sensitive web
application is hosted on `A.example1.com`, you should ideally host Synapse on
`example2.com`. Some amount of protection is offered by hosting on
`B.example1.com` instead, so this is also acceptable in some scenarios.
However, you should *not* host your Synapse on `A.example1.com`.

Note that all of the above refers exclusively to the domain used in Synapse's
`public_baseurl` setting. In particular, it has no bearing on the domain
mentioned in MXIDs hosted on that server.

Following this advice ensures that even if an XSS is found in Synapse, the
impact to other applications will be minimal.
