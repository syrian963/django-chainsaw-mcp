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
| `LICENSE` | the full MIT licence text, unmodified |
| `NOTICE` | copyright line, plain-language summary, attribution requirement |
| `CITATION.cff` | GitHub renders a "Cite this repository" button from it |
| SPDX headers | two lines at the top of every source file |

The SPDX headers matter more than they look. `LICENSE` protects the repository;
the header travels with a **single file** somebody copies out of it, which is
the usual way attribution disappears.

```python
# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT
```

## How this was written

Claude wrote most of the lines. I decided what to build, rejected what was not
worth building, reviewed every change, and ran the measurements that settled
the arguments. Saying that plainly is cheaper than having somebody work it out
later.

What that looked like in practice, because the distinction matters more than
the label:

- **Two finished features were deleted after measurement.** An untyped
  comparison route in the `choices` check produced five false positives out of
  five on real code. Min/Max/Avg in the aggregates check are not affected by
  the row duplication it looks for, so flagging them was wrong. Both worked.
  Both went.
- **A call-graph rule was rejected before it was finished** because measuring
  it first showed it would resolve 8% of unknown calls and attribute 25 more
  findings. Not worth the complexity.
- **The tool was pointed at eighteen public projects**, and each defect they
  exposed is in the changelog with the number that found it. Twelve of them.
- **Wrong claims of my own were corrected in public**: a performance figure
  taken from a contended machine, a fix whose first version suppressed a real
  finding, a red-green check that passed for the wrong reason three times in
  one file.

Judgement is the part that does not come free, and it is the part visible in
the history: what was measured before it was built, what was deleted after it
was measured, and what is documented as still unknown.

## Why MIT

This started under AGPL-3.0, which was the cautious choice: it is the only
common licence that also covers software people reach over a network, and it
makes commercial appropriation legally awkward. It is also the licence a great
many companies will not let their engineers depend on at all.

The trade was written down here before it was made, and it decided this:

| | AGPL-3.0 | MIT |
| --- | --- | --- |
| Use it inside a company | fine | fine |
| Ship a modified version as a service | must publish the changes | no obligation |
| A company with a licence policy | usually blocked outright | almost always allowed |
| Somebody selling your work | must open their changes | may, with attribution |

**Control was not the goal here; being used was.** A tool nobody is allowed to
depend on does not get the bug reports that made this one worth anything -
eighteen public projects found twelve defects in it, and the next twelve will
come from people running it on code I will never see. AGPL is a good way to
lose those people at the dependency review.

The one obligation MIT keeps is the one that matters for authorship: the
copyright notice travels with the copy. Every source file carries an SPDX
header saying so, which is what survives somebody lifting a single file.

**The direction is the easy one.** Going from AGPL to MIT is a decision a sole
copyright holder can simply make. Going back is not: every copy released under
MIT stays MIT forever, and only new work could be relicensed. That asymmetry is
worth knowing, and it is the reason this was not the starting position.

## Signed commits, the part that is actual proof

A git author line is just text. Anybody can set `user.name` to any name and
write commits that claim to be from anybody. **A signature cannot be forged
without the private key.**

Signing is set up. An ed25519 key, `commit.gpgsign` and `tag.gpgsign` on, and
the public half registered on the account as a *signing* key:

```bash
ssh-keygen -t ed25519 -C "you@example.com" -f ~/.ssh/id_ed25519_signing
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519_signing.pub
git config --global commit.gpgsign true
git config --global tag.gpgsign true
git log --show-signature -1
```

**The part that is easy to get wrong.** GitHub does not check the signature
against the key you registered. It looks up the account that owns the
**commit's email address**, and then looks for a signing key on *that*
account. The first signed commit here came back `unknown_key` with a perfectly
valid signature, because the commit email belonged to one account and the key
had been added to another - the one that owns this repository, the PyPI
package and the trusted publisher.

So the commit email is set per repository to an address verified on the
account that owns it, rather than the global one:

```bash
git config user.email "the-address-verified-on-that-account"
```

`gh api repos/OWNER/REPO/commits/HEAD --jq .commit.verification` says which of
the two is wrong: `unknown_key` means the account has no signing key,
`unverified_email` means the address is not on any account.

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

The strongest single item on that list is the first one, and it is what this
repository actually has: the reasoning, the wrong turns, the measurements that
contradicted them, and the features deleted afterwards. A copy has one commit
and no argument in it.

It is also the honest answer to *how much of this did you write*. The history
does not hide that Claude wrote most of the lines, and it does not need to -
what it shows is which of them survived contact with a measurement.
