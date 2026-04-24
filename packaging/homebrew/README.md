# Homebrew Tap Release Guide

This project uses a custom tap for Homebrew distribution.

## End User Install

```bash
brew tap xbunax/tap
brew install xbunax/tap/agent-tmux-notify
```

## Maintainer Release Flow

1. Create and push a Git tag in this repository, for example `v0.1.0`.
2. Create a GitHub Release from that tag.
3. Download the release tarball checksum:

```bash
curl -L https://github.com/xbunax/agent-tmux-notify/archive/refs/tags/v0.1.0.tar.gz | shasum -a 256
```

4. Update `packaging/homebrew/agent-tmux-notify.rb`:
   - `url` to the new tag tarball URL
   - `sha256` to the checksum from step 3
5. Copy the formula file into tap repository path `Formula/agent-tmux-notify.rb`.
6. Commit and push in the tap repository (`xbunax/homebrew-tap`).

## Local Validation

```bash
brew uninstall --ignore-dependencies agent-tmux-notify || true
brew tap xbunax/tap
brew install xbunax/tap/agent-tmux-notify
agent-tmux-notify --help
brew test xbunax/tap/agent-tmux-notify
```
