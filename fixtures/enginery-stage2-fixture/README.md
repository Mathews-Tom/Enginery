# enginery-stage2-fixture

Disposable Stage 2 release-provider proof fixture for [Enginery](https://github.com/Mathews-Tom/Enginery).

This package exists solely to exercise Enginery's Stage 2 plan-to-release
workflow — root-to-leaf merge, version/changelog preparation, wheel/sdist
build, and real GitHub Release + PyPI publication — end to end against a
real, disposable public artifact.

It is not a usable library, carries no functional guarantees, and its
version history exists only to prove the release mechanism works. It
never shares a distribution name or version namespace with the `enginery`
product package itself.
