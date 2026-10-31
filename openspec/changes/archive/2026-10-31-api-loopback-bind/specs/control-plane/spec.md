# Spec Delta

## ADDED Requirements

### Requirement: API listen address
The API SHALL listen on `127.0.0.1` unless `CURATE_D_API_LISTEN_HOST` names another address.
The value MUST be an IPv4 or IPv6 literal; a host name, an empty value or any other string MUST
stop the API at startup, before it binds, with the variable and the value named. The container
image MUST set `CURATE_D_API_LISTEN_HOST=0.0.0.0`, so a container started from it listens on all
of its interfaces.

#### Scenario: Loopback when unset
- **WHEN** the API starts with `CURATE_D_API_LISTEN_HOST` unset
- **THEN** it listens on `127.0.0.1` and on no other interface

#### Scenario: The variable names the address
- **WHEN** the API starts with `CURATE_D_API_LISTEN_HOST` set to `0.0.0.0` or `::`
- **THEN** it listens on that address

#### Scenario: An invalid value stops the API
- **WHEN** the API starts with `CURATE_D_API_LISTEN_HOST` set to `localhost`, `""` or `0.0.0.0:8000`
- **THEN** startup fails with an error naming `CURATE_D_API_LISTEN_HOST` and the value
- **AND** the API binds no socket

#### Scenario: The environment label does not choose the address
- **WHEN** the API starts with `CURATE_D_ENVIRONMENT` `local`, `dev`, `prod` or unset, and `CURATE_D_API_LISTEN_HOST` unset
- **THEN** it listens on `127.0.0.1`

#### Scenario: A container from the image listens on all its interfaces
- **WHEN** a container runs the image with no `CURATE_D_API_LISTEN_HOST` of its own
- **THEN** the API listens on `0.0.0.0`
- **AND** the image's published or Service-routed port reaches it
