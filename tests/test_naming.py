"""Studio / cover names must not double-stamp a serial RSS title prefix.

`publish_series` requires serial titles to be `EP{NN}. 正文` (players hide
`itunes:episode`). The outline title is immutable, so that prefix has to be
present at generation time. The naming iron rule is still `EP{NN} 正文` — the
label and the cover badge already carry the episode number.
"""

from notebooklm_mcp.naming import bare_episode_title, episode_label


def test_bare_title_passthrough():
    assert bare_episode_title(1, "心法篇") == "心法篇"


def test_bare_strips_a_matching_dotted_prefix():
    assert bare_episode_title(1, "EP01. 心法篇") == "心法篇"


def test_bare_strips_a_matching_plain_prefix():
    """Idempotent on the iron-rule form so re-labeling a label is a no-op."""
    assert bare_episode_title(1, "EP01 心法篇") == "心法篇"


def test_bare_leaves_another_episode_prefix_alone():
    """Wrong-number prefixes are a publish-gate problem, not a naming one."""
    assert bare_episode_title(2, "EP07. 心法篇") == "EP07. 心法篇"


def test_label_from_a_bare_title():
    assert episode_label(1, "心法篇") == "EP01 心法篇"


def test_label_from_a_serial_rss_title():
    assert episode_label(1, "EP01. 心法篇") == "EP01 心法篇"


def test_label_is_idempotent_on_itself():
    assert episode_label(1, "EP01 心法篇") == "EP01 心法篇"


def test_label_does_not_eat_the_wrong_episode_number():
    assert episode_label(1, "EP07. 心法篇") == "EP01 EP07. 心法篇"


def test_label_when_the_title_is_only_the_prefix():
    assert episode_label(1, "EP01. ") == "EP01"
