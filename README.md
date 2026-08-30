# Safety-Critical Rust Coding Guidelines

This repository contains Coding Guidelines for writing Safety Critical Rust, developed by the [Safety Critical Rust Consortium][safety-critical-rust-consortium].

- View the [rendered guidelines](https://coding-guidelines.arewesafetycriticalyet.org/) online.
- Check out the [project goals](GOALS.md).

## Building the coding guidelines

The Safety-Critical Rust Coding Guidelines website uses `Sphinx` and `Sphinx-Needs` to build a rendered version of the coding guidelines, and `uv` to install and manage Python dependencies (including Sphinx itself). To simplify building the rendered version, we created a script called `make.py` that takes care of invoking Sphinx with the right flags.

Build the rendered version by running:

```shell
uv run --frozen make.py
```

The required uv version is pinned in `pyproject.toml`. Until a tooling owner is
assigned, periodic uv upgrades are a shared maintainer responsibility: update
the uv runtime pin, the `uv_build` range, and `uv.lock` together in one PR.

By default, Sphinx uses incremental rebuilds to generate the content that
changed since the last invocation. If you notice a problem with incremental
rebuilds, pass the `-c` flag to clear the existing artifacts before
building `uv run --frozen make.py -c`.

The following output is generated:

- A rendered version in `build/html/`
- A machine-parseable artifact in `build/html/needs.json`
- A record of the contents with checksums in `build/html/guidelines-ids.json`

<!-- TODO: Pete LeVasseur The `needs.json` file could use some cleaning up and some description here of the contents. -->

### Running builds offline

If you're working without internet access or want to avoid reaching out to remote resources, you can pass the `--offline` flag:

```shell
uv run --frozen make.py --offline
```

This prevents the build system from attempting to fetch remote resources, such as updates to the specification. Use this flag when you need reproducible or air-gapped builds.

Use `--offline` if you are running `make.py` frequently during development, to prevent rate-limiting due to repeated requests to the [the FLS](https://rust-lang.github.io/fls/paragraph-ids.json).

### Checking an out-of-date spec lock file

It is fairly common for `src/spec.lock` to become outdated while a contributor is developing an unrelated guideline.

Local and normal CI builds report this drift without failing solely because of it. Missing or malformed lock data and invalid FLS references still fail the build.

CI enforcement differs by workflow; see the [FLS CI enforcement policy](docs/fls-audit.md#ci-enforcement-policy) for the blocking and nonblocking paths.

Release maintainers must run `Release Preflight` with the exact intended commit
SHA before pushing a version tag. First publication requires a successful
preflight from the preceding 24 hours; see the
[release procedure](docs/fls-audit.md#release-preflight-and-deploy).

#### Enforcing freshness locally

Nightly and Release Preflight enforce freshness. To run the same check locally:

```shell
uv run --frozen make.py --enforce-spec-lock-diff
```

#### Auditing the difference

When the build detects a difference in `spec.lock`, a log is saved in `/tmp/fls_diff_<random>.txt` which you can use to audit the differences.

To see a quick summary of the difference:

```shell
uv run python scripts/fls_audit.py --summary-only
```

To see a full report of the difference:

```shell
uv run python scripts/fls_audit.py
```

See [FLS audit docs](docs/fls-audit.md) for the full workflow, snapshots, advanced options, and
the steps to rationalize and update `src/spec.lock`, including the rationalization checklist.

## What we're working on

The Coding Guidelines [work items board](https://github.com/orgs/Safety-Critical-Rust-Consortium/projects/4) shows tickets actively being worked on, and tickets you can pick up.

## Contributing

Read the [CONTRIBUTING.md](./CONTRIBUTING.md) and [REVIEWING.md](./REVIEWING.md) for the details on contributing and reviewing guidelines.

## [Code of Conduct][code-of-conduct]

The [Rust Foundation][rust-foundation] has adopted a Code of Conduct that we
expect project participants to adhere to. Please read [the full
text][code-of-conduct] so that you can understand what actions will and will not
be tolerated.

## Licenses

Rust is primarily distributed under the terms of both the MIT license and the
Apache License (Version 2.0), with documentation portions covered by the
Creative Commons Attribution 4.0 International license..

See [LICENSE-APACHE](LICENSE-APACHE), [LICENSE-MIT](LICENSE-MIT),
[LICENSE-documentation](LICENSE-documentation), and
[COPYRIGHT](COPYRIGHT) for details.

You can also read more under the Foundation's [intellectual property
policy][ip-policy].

## Other Policies

Read other Rust Foundation [policies][foundation-website].

[code-of-conduct]: https://foundation.rust-lang.org/policies/code-of-conduct/
[foundation-website]: https://foundation.rust-lang.org
[ip-policy]: https://foundation.rust-lang.org/policies/intellectual-property-policy/
[media-guide and trademark]: https://foundation.rust-lang.org/policies/logo-policy-and-media-guide/
[rust-foundation]: https://foundation.rust-lang.org/
[safety-critical-rust-consortium]: https://github.com/Safety-Critical-Rust-Consortium/safety-critical-rust-consortium
