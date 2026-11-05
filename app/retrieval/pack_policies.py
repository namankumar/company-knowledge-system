"""Declared, coverage-enforced context-pack policies.

Direct analog of app/llm/policies.py's TaskPolicy, but for
build_authorized_context_pack(). A `purpose` with no registered PackPolicy
cannot build a pack: get_pack_policy() raises PackPolicyMissingError instead
of silently falling back to the old flat priority+score sort. This is the
enforcement mechanism, not a suggestion --
tests/test_pack_policy_coverage.py AST-walks app/ for every literal
`purpose=` passed to build_authorized_context_pack and asserts it has an
entry here, mirroring tests/test_llm_task_policy_coverage.py's approach for
call_llm(task=...).

A ViewSpec wraps one of retrieve.py's *existing* retrieval loaders
(_open_risk_objects, _stale_work_objects, _upcoming_obligations,
_recent_contradiction_facts, and Slice 3's workstream-digest loader). This
module adds no new retrieval logic, only a declared token-budget floor per
view: a view's floor is a reservation, not a suggestion. An empty view
yields its floor tokens back to ranked fill (Tier 2); a non-empty view
within its floor is never evicted by another view's items, which is exactly
the silent-crowding-out failure class this registry exists to close.

Loaders are registered by app/retrieval/retrieve.py (and, once it exists,
app/personalization/workstream_digest.py) via register_view_loaders() after
their own functions are defined, rather than imported here directly --
retrieve.py imports PackPolicy/ViewSpec/get_pack_policy from this module, so
a module-level import in the other direction would be circular.
"""
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

ViewLoader = Callable[..., Awaitable[list[dict]]]

DigestPreference = Literal["prefer_digest", "prefer_raw", "digest_only"]

PACKPOLICY_VERSION = "packpolicy-v1"


class PackPolicyMissingError(Exception):
    """No PackPolicy is registered for the requested purpose.

    Fail loud, not fail open -- mirrors call_llm()'s "no TaskPolicy -> raise"
    behavior (app/llm/client.py). There is no more silent fallback to the old
    purpose in (...) branching in build_authorized_context_pack.
    """


@dataclass(frozen=True)
class ViewSpec:
    """One named retrieval view a PackPolicy loads, with a reserved token
    floor. `item_cap` is an optional cap independent of the token floor
    (retrieve.py's OBJECT_LIMIT/CHUNK_LIMIT/FACT_LIMIT constants become
    per-view caps here instead of always-global limits)."""

    name: str
    loader: ViewLoader
    floor_tokens: int
    item_cap: int | None = None


@dataclass(frozen=True)
class PackPolicy:
    purpose: str
    views: tuple[ViewSpec, ...] = ()
    token_budget: int | None = None
    ranked_fill_enabled: bool = True
    digest_preference: DigestPreference = "prefer_raw"
    policy_version: str = PACKPOLICY_VERSION


# Populated by register_view_loaders() once retrieve.py's loader functions
# exist. Empty until then -- see the module docstring for why.
PACK_POLICIES: dict[str, PackPolicy] = {}

_VIEW_LOADERS: dict[str, ViewLoader] = {}

# The full set of named loaders every registered PackPolicy view may draw
# on. "digests" is Slice 3's workstream-digest view: declared and available
# to any PackPolicy from the moment workstream_digest.py registers it, but
# not yet added to any purpose's `views` tuple below (per the packet's
# Slice 3 scope note: "wired ... not activated broadly yet, just
# available").
_REQUIRED_LOADER_NAMES = {
    "open_risk",
    "stale_work",
    "upcoming_obligations",
    "contradictions",
    # Slice A.2 (architecture-packet-artifacts-lifecycle-2026-07-11.md):
    # current-status artifacts as a pack-item view. Required (not optional
    # like "digests" below) because it's wired into a purpose's `views`
    # tuple in this same pass, not merely declared for future opt-in.
    "artifact",
}


def register_view_loaders(**loaders: ViewLoader) -> None:
    """Register one or more named view loaders. Safe to call more than once
    (e.g. once from retrieve.py, later from workstream_digest.py) -- the
    registry is rebuilt each time against the accumulated loader set."""
    _VIEW_LOADERS.update(loaders)
    _build_registry()


def _build_registry() -> None:
    if not _REQUIRED_LOADER_NAMES.issubset(_VIEW_LOADERS):
        return  # not fully registered yet; retrieve.py hasn't finished loading

    open_loops_view = ViewSpec(
        "open_loops", _VIEW_LOADERS["open_risk"], floor_tokens=3000, item_cap=25
    )
    upcoming_view = ViewSpec(
        "upcoming_obligations", _VIEW_LOADERS["upcoming_obligations"], floor_tokens=2000, item_cap=25
    )
    contradictions_view = ViewSpec(
        "contradictions", _VIEW_LOADERS["contradictions"], floor_tokens=1500, item_cap=30
    )
    stale_work_view = ViewSpec(
        "stale_work", _VIEW_LOADERS["stale_work"], floor_tokens=1500, item_cap=25
    )
    # Slice A.2: floor_tokens=0 is the mechanism that achieves "unfloored
    # ranked-fill" rather than "always-included floor item" -- every item
    # this view's loader returns costs > 0 tokens, so it can never fit
    # inside a 0-token floor and always falls through to the shared
    # ranked-fill pool (see build_authorized_context_pack's per-view loop:
    # `if view_tokens_used + cost > view.floor_tokens: _add(items, ...)`).
    # This is the packet's Q3 binding resolution ("start unfloored in
    # Tier 2, revisit on yield data"), not a stand-in for a real floor.
    artifact_view = ViewSpec(
        "artifact", _VIEW_LOADERS["artifact"], floor_tokens=0, item_cap=25
    )
    digest_view = (
        ViewSpec("digests", _VIEW_LOADERS["digests"], floor_tokens=6000, item_cap=40)
        if "digests" in _VIEW_LOADERS else None
    )

    policies: dict[str, PackPolicy] = {
        "drift": PackPolicy(
            purpose="drift",
            views=(open_loops_view, upcoming_view, contradictions_view),
        ),
        "brief_convergence": PackPolicy(
            purpose="brief_convergence",
            views=tuple(
                view
                for view in (digest_view, open_loops_view, upcoming_view, contradictions_view)
                if view is not None
            ),
            token_budget=16000,
            digest_preference="prefer_digest",
        ),
        "brief": PackPolicy(
            purpose="brief",
            views=(open_loops_view, stale_work_view, upcoming_view, contradictions_view, artifact_view),
            token_budget=30000,  # matches brief.py's BRIEF_PACK_TOKEN_BUDGET
        ),
        "brief_write": PackPolicy(
            purpose="brief_write",
            views=(artifact_view,),
            token_budget=30000,
            digest_preference="prefer_raw",
        ),
        "brief_weekly": PackPolicy(purpose="brief_weekly", views=()),
        # Slice A.2: chat is the other surface the packet's API Surfaces
        # section names as gaining artifact citations ("existing brief and
        # chat endpoints ... can now include artifact:{id}-sourced
        # citations"). drift, personalization_seed, and every other purpose
        # deliberately do NOT get this view -- the packet scopes it to brief
        # and chat only.
        "chat": PackPolicy(purpose="chat", views=(artifact_view,)),
        "search": PackPolicy(purpose="search", views=()),
        # Slice 3.2 (threads-sessions packet, Track C): the session tool
        # loop's `search`/`dig` tools build a pack the same way `chat`/
        # `search` already do -- ad hoc, query- or object_id-scoped, no
        # floor views of their own. Distinct purpose names (not reuse of
        # "chat"/"search") so citation-yield instrumentation and any future
        # per-purpose tuning can distinguish session-tool-loop pulls from
        # the existing chat/search surfaces.
        "session_search": PackPolicy(purpose="session_search", views=()),
        "session_dig": PackPolicy(purpose="session_dig", views=()),
        "personalization_seed": PackPolicy(purpose="personalization_seed", views=()),
        "tenant_context_seed": PackPolicy(purpose="tenant_context_seed", views=()),
        "person_context_seed": PackPolicy(purpose="person_context_seed", views=()),
        "person_context_update": PackPolicy(purpose="person_context_update", views=()),
        "artifact_verify": PackPolicy(purpose="artifact_verify", views=()),
        # Slice 3: the pack built for the digest-drafting LLM call itself.
        # Its evidence bundle is code-selected and ACL-intersected per
        # audience_scope *before* this call (see
        # app/personalization/workstream_digest.py), passed in via
        # object_ids/query the same way any other targeted pack is built --
        # it does not need a floor view of its own, so `views` stays empty.
        # This keeps invariant 2 intact for the LLM-facing step: assembly
        # still goes through build_authorized_context_pack, even though
        # evidence *selection* upstream of it is scope-specific code.
        "workstream_digest_draft": PackPolicy(purpose="workstream_digest_draft", views=(), token_budget=4000),
    }
    # "digests" is Slice 3's workstream-digest pack-item view: once
    # workstream_digest.py registers a loader under this name, it becomes
    # available for any *other* PackPolicy (e.g. brief, drift) to opt into
    # by adding a `digests` ViewSpec to its `views` tuple -- not activated
    # broadly in this pass, per the packet's stated Slice 3 scope ("wired
    # ... not activated broadly yet, just available"). Declared here so a
    # future PackPolicy change is a one-line addition, not new plumbing.
    PACK_POLICIES.clear()
    PACK_POLICIES.update(policies)


def _ensure_registry_built() -> None:
    if PACK_POLICIES:
        return
    # Self-healing fallback: the registry is only populated once
    # app.retrieval.retrieve has been imported (it registers its loaders at
    # module-bottom, see that module). Most production/API call sites import
    # retrieve.py first anyway, but a caller or test that only imports this
    # module directly would otherwise see an empty registry purely due to
    # import order, not a real coverage gap. Importing retrieve.py here is
    # deferred (function-local), not a module-level import, so it does not
    # reintroduce the circular import this module's docstring describes.
    import app.retrieval.retrieve  # noqa: F401


def get_pack_policy(purpose: str) -> PackPolicy:
    _ensure_registry_built()
    policy = PACK_POLICIES.get(purpose)
    if policy is None:
        raise PackPolicyMissingError(
            f"no PackPolicy registered for purpose={purpose!r}; add an entry in "
            "app/retrieval/pack_policies.py's registry builder before calling "
            "build_authorized_context_pack with this purpose."
        )
    return policy
