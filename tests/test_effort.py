from skill_advisor import effort


def test_enums_are_asymmetric():
    assert effort.ULTRACODE in effort.RECOMMENDABLE
    assert effort.ULTRACODE not in effort.OBSERVABLE
    assert effort.MAX in effort.OBSERVABLE
    assert effort.MAX not in effort.RECOMMENDABLE


def test_rank_ordering():
    assert effort.rank(effort.LOW) < effort.rank(effort.MEDIUM)
    assert effort.rank(effort.MEDIUM) < effort.rank(effort.HIGH)
    assert effort.rank(effort.HIGH) < effort.rank(effort.XHIGH)
    assert effort.rank(effort.XHIGH) < effort.rank(effort.MAX)
    # ultracode resolves to xhigh effort, so it ranks equal to xhigh
    assert effort.rank(effort.ULTRACODE) == effort.rank(effort.XHIGH)


def test_rank_unknown_is_none():
    assert effort.rank("turbo") is None
    assert effort.rank("") is None


def test_to_persistable():
    assert effort.to_persistable(effort.ULTRACODE) == effort.XHIGH
    assert effort.to_persistable(effort.MAX) is None
    assert effort.to_persistable(effort.HIGH) == effort.HIGH
    assert effort.to_persistable("turbo") is None


def test_should_nudge_on_disagreement():
    assert effort.should_nudge(effort.MEDIUM, effort.XHIGH) is True
    assert effort.should_nudge(effort.XHIGH, effort.MEDIUM) is True


def test_should_not_nudge_on_agreement():
    assert effort.should_nudge(effort.HIGH, effort.HIGH) is False
    # ultracode ranks equal to xhigh, so being at xhigh already satisfies it
    assert effort.should_nudge(effort.XHIGH, effort.ULTRACODE) is False


def test_max_silences_the_feature():
    # The user has deliberately gone above anything we know how to recommend.
    assert effort.should_nudge(effort.MAX, effort.LOW) is False
    assert effort.should_nudge(effort.MAX, effort.ULTRACODE) is False


def test_missing_observation_suppresses_nudge():
    # First prompt of a session: sensor has not run yet. Never guess.
    assert effort.should_nudge(None, effort.XHIGH) is False
    assert effort.should_nudge(effort.LOW, None) is False
