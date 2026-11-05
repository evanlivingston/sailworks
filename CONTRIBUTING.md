# Contributing

## Bug report and feature suggestion

If you have found an issue with sailworks, first do a search in the [sailworks issue tracker](https://github.com/evanlivingston/sailworks/issues) to ensure that there are no existing issues with
similar content. If you can't find another related issue, open a new one: please try to be as detailed as possible.


## Submit a pull request

If you want to contribute to sailworks, you have to follow this flow:

1. First fork the [sailworks repository](https://github.com/evanlivingston/sailworks) so you can work on your copy of the code
2. Clone it locally by running ```git clone https://github.com/YOUR_USERNAME/sailworks.git```

We also suggest to use `pyenv` to create an isolated pip environment:

- Create a new env: ```pyenv virtualenv sailworks-env```
- Activate it: ```pyenv activate sailworks-env```


### Test environment

The test requirements are declared once, in the `test` dependency group in
`pyproject.toml`. tox reads that group and installs the project for its runtime
dependencies, so there is no second list to keep in step.

```bash
uv sync --group test   # runtime + test dependencies into .venv
python -m pytest -q    # run the suite
tox -e unit-tests      # the same suite in tox's isolated environment
```

`pyais`, `eccodes`, `eccodeslib`, `gcsfs` and `google-auth` are part of that
group; without them parts of the suite fail for missing imports rather than for
real defects.

### Working on the code

1. Now that you have your local copy of sailworks, you can start working on the code; first, search or create the issue you want to contribute to in the sailworks issue tracker, and ask
to be assigned to that issue in order to avoid duplicate work.

2. Create e new branch with a descriptibe name where you will do the changes; the starting point is always `origin/dev`:

```bash
git fetch origin
git checkout -b your-branch-name origin/dev
```

3. Before each commit, use tox (```pip install tox```) in order to check that typechecker, linters and tests run fine:

```tox```

Use short and descriptive commit message, and remember to add tests to check new functionalities.

Every commit must be signed off under the Developer Certificate of Origin (DCO, https://developercertificate.org).
Add the trailer with `git commit -s`; it certifies that you wrote the change or have the right to submit it
under this project's license (`GPL-3.0-or-later`). Contributions without a `Signed-off-by` trailer are not merged.


4. If your branch drifts out of sync from `origin/dev`, you can fetch and merge the updated version:

```git pull origin dev```

5. Finally push your code in your branch

```git push origin your-branch-name```

6. Once you're happy with your changes, visit [sailworks github](https://github.com/evanlivingston/sailworks) and start a new pull request from `USERNAME:your-branch-name` into `evanlivingston:dev`. Maintainers will start reviewing your code and they may ask you to do some changes (in your branch): PR updates automatically every time you push to your working branch.


### Third-party software and reverse engineering

Studying how another product behaves is fine; shipping its expression is not. Do not
commit, attach or paste any of the following:

- application binaries, bundles, DLLs or installer contents from any third-party product;
- decompiler, disassembler or debugger output, pseudocode, strings dumps or hex dumps;
- extracted assets or data files (icons, palettes, polars, precomputed tables, sample data);
- code transcribed from another product's implementation.

Describe the observed behavior and the interface or file format involved, and implement it
independently. Analysis notes belong in `docs/history/` with their evidence boundary stated;
implementation must be written from the behavior, not from recovered code. If you are unsure
whether a change crosses that line, ask before opening the pull request.

### Running CI locally

Install `act` and run your workflow:

```bash
act -j build-appimage
```


### Make docs

You can also create docs locally in order to check if everything is as expected:

```bash
pip install sphinx sphinx_rtd_theme sphinx_rtd_dark_mode myst_nb
make docs
```


## Publish a new release (maintainers only)

1. Create the release on github
2. Wait for the CI to build the appimage file
3. Upload the appimage in the new release
4. Update website appimage link

### Flatpak
1. Pin the release tag in the flatpak manifest
2. Build the release
3. flatpak-pip-generator --runtime=org.gnome.Sdk//49 -r requirements.txt --yaml
