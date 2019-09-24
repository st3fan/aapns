# Changelog

## (unreleased)

* Removed `aapns.connect`
* Removed all import aliases from `aapns`
* Removed custom HTTP/2 client (replaced with [httpx](https://github.com/encode/httpx))
* Added `aapns.api.create_client` to instantiate a connection to APNS.
* Added full, [mypy](http://www.mypy-lang.org) verified, type hints
* Added [black](https://github.com/psf/black) formatting
* Changed build system from setuptools to poetry