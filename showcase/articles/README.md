# Article review snapshots · 2026-11-10

These are versioned reference snapshots of articles owned by the author's personal article library. Edit the canonical library article first, then explicitly refresh the snapshot. Content hashes are in each snapshot.json. This commit does not publish either article or change remote access.

- **Recreating Harness²:** synced from the current Forge report, replacing the stale local prose. All127 text blocks,16 images and5 native panel groups accounted for. Native panels are links in Markdown; their lossless configuration and the prior local revision remain in the canonical library. The sync receipt preserves source identity and block hashes. Existing image bytes were unchanged;7 images were newly mirrored locally. The remote report was not edited.
- **Before we let an agent optimize the harness:** review draft based on completed baselines, repeatability and sampling controls. No agent-driven optimization result is claimed. Its detailed experiment records remain under results/ and protected state; the calculation download contains only ordinal scores and aggregates.

## Reproduce and preview

Run `python3 meta-harness-controls/evidence/reproduce.py` to reproduce the headline means and all5 paired intervals, including the overlap sensitivity and secondary first-sample comparison. No external dependencies, network or model calls.

To rebuild HTML previews, install requirements-preview.txt into an isolated environment and run `python render_previews.py`. To regenerate compact mobile figures, use the pinned dependencies in ../requirements-figures.txt and run `python meta-harness-controls/render_narrow_figures.py`. These use the saved numeric evidence only.

Open index.html for both articles. Desktop figures retain the previously verified values; responsive HTML uses compact figures and labeled stacked tables on narrow screens. The analysis ZIP is an allowlisted copy of evidence/; it excludes raw prompts, labels, responses, content hashes, source paths and private trace links. The existing published Harness² reference retains its original public evidence destinations, some of which require sign-in.

Validation: canonical article validators passed, all local links resolve, the isolated analysis ZIP reproduces its estimates, all report prose blocks match the fetched revision, all16 report images loaded, and desktop1280px/narrow390px layouts were inspected. Core experiment code is unchanged; the prior affected-test run remains documented with the experiment results.
