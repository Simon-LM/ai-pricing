"""Test package.

Empty, and load-bearing: it lets a caller name one test module rather than
discovering them all. The weekly refresh workflows use that to run the shared
tests plus their own provider's, so that a broken fixture in one provider can no
longer stop the other three from publishing -- which is what happened on
2026-09-07, when a single new OVH model took Eden AI and Hugging Face down with it
for a week. `unittest discover -s tests` still finds everything, and is what runs
on every push.
"""
