This self-signed certificate and deliberately public private key are synthetic MIT-licensed test data. Never use them outside local negative TLS tests. They were generated with:

    openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 3650 -subj '/CN=localhost'

Tests verify that generated actions reject this untrusted certificate even though the upstream HTTP client temporarily changes NODE_TLS_REJECT_UNAUTHORIZED. The key is not a credential for any service.
