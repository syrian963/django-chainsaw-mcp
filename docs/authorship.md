# Authorship and licence

## What is already true

**Copyright exists from the moment the code is written.** Under German law
(`§ 2 UrhG`) it arises on creation, with no registration and no fee. There is no
copyright register in Germany to file with, and none is needed.

So the question is not *how do I get rights*, it is *what can I prove, and what
have I allowed*.

## What is in place

| File | Does what |
| --- | --- |
| `LICENSE` | the full AGPL-3.0 text, unmodified |
| `NOTICE` | copyright line, plain-language summary, attribution requirement |
| `CITATION.cff` | GitHub renders a "Cite this repository" button from it |
| SPDX headers | two lines at the top of every source file |

The SPDX headers matter more than they look. `LICENSE` protects the repository;
the header travels with a **single file** somebody copies out of it, which is
the usual way attribution disappears.

```python
# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
```

## Why AGPL-3.0, and what it costs

The ordinary GPL is triggered by **distributing** software. A great deal of
software is never distributed: it runs on a server and users only see its
output. Section 13 of the AGPL closes that gap. Anyone who lets users interact
with a modified version over a network must offer those users the source.

| | Obligation |
| --- | --- |
| Reading it, using it, changing it for yourself | none |
| Running it inside a company on your own code | none |
| Distributing it, or offering a modified version as a service | publish your modifications under the same licence |
| Removing the copyright notices | never permitted |

**The honest trade.** AGPL keeps control and deters commercial appropriation. It
also deters corporate users and contributors: many companies have a blanket ban
on AGPL dependencies. Under MIT this project would be forked and shipped inside
a paid product with no obligation beyond keeping a copyright line.

- **Control is the goal** → AGPL, which is what is set.
- **Adoption and reputation are the goal** → MIT or Apache-2.0 get more of both.

Switching from AGPL to MIT later is easy while there is a single copyright
holder. Switching from MIT to AGPL is not: every existing copy stays MIT
forever. **Starting restrictive is the reversible choice**, which is the other
reason it is set this way.

## Dual licensing stays open

The copyright holder is not bound by their own licence. AGPL for the public,
and a separate commercial licence for a company that wants to use it without the
source obligation. That model only works if the AGPL version is the public one
from the beginning, which it now is.

It also requires that every contributor either assigns copyright or signs a CLA,
otherwise the commercial licence cannot be granted. Worth remembering before
merging the first outside pull request.

## Signed commits, the part that is actual proof

A git author line is just text. Anybody can set `user.name` to any name and
write commits that claim to be from anybody. **A signature cannot be forged
without the private key.**

There is no signing key on this machine yet. Creating cryptographic identity
material is a decision for the person it identifies, so here are the commands
rather than a key that was generated for you.

```bash
ssh-keygen -t ed25519 -C "mnouralsakka@gmail.com" -f ~/.ssh/id_ed25519_signing
```

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519_signing.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true
```

Then add the **public** key to GitHub twice: once under *SSH keys* as a signing
key, and once as an *authentication* key if it will also be used for pushing.
Commits then show a **Verified** badge, and that badge is not something a copier
can reproduce.

```bash
git log --show-signature -1
```

Existing commits stay unsigned. Rewriting history to sign them retroactively is
possible and not worth it: the signed ones from here on establish the pattern,
and the unsigned ones are already timestamped on the remote.

## What actually protects the work

Ranked by how much they help, which is not the order people expect:

1. **A public repository with a real history.** Dozens of commits over weeks,
   each explaining a decision, is evidence no thief can manufacture after the
   fact. A stolen copy has one commit and an empty history.
2. **Signed commits.** Cryptographic, not clerical.
3. **A restrictive licence.** Makes commercial use legally hard rather than
   physically impossible.
4. **SPDX headers.** Survive file-level copying.
5. **PyPI publication.** A timestamped public record under a name only the owner
   controls.

**None of them prevent copying.** Anything readable is copyable. What they do is
make the copy obviously derivative and the theft provable, and that is the
realistic goal.

The strongest single item on that list is the first one, and it is already
there: this repository's history contains the reasoning, the wrong turns, and
the bugs found along the way. That is very hard to fake and very easy to
demonstrate.
