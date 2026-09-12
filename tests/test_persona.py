"""The canonical Jeles persona ships in-package and loads via load_persona()."""

from __future__ import annotations

import json

import jeles


def test_persona_path_points_into_package():
    p = jeles.persona_path()
    assert p.exists()
    assert p.name == "jeles_persona.json"
    assert p.parent.name == "persona"


def test_load_persona_returns_the_jeles_identity():
    persona = jeles.load_persona()
    assert isinstance(persona, dict)
    assert persona["identity"]["name"] == "Jeles"
    # A few load-bearing sections the persona is defined by.
    for section in ("identity", "voice", "boundaries", "non_negotiable"):
        assert section in persona


def test_load_persona_matches_the_shipped_file():
    persona = jeles.load_persona()
    raw = json.loads(jeles.persona_path().read_text(encoding="utf-8"))
    assert persona == raw


def test_load_persona_is_cached():
    assert jeles.load_persona() is jeles.load_persona()


# -- persona_prompt: the combine (#18) -------------------------------------


def test_persona_prompt_renders_a_string():
    p = jeles.persona_prompt()
    assert isinstance(p, str) and p.startswith("You are Jeles")
    assert jeles.persona_prompt() is jeles.persona_prompt()  # cached


def test_persona_prompt_is_a_superset_of_the_old_prose():
    # Every distinctive beat the hand-authored ask-jeles/utety-chat prose carried
    # must survive the combine into the compiled prompt — nothing lost.
    p = jeles.persona_prompt()
    for beat in (
        "misfiled",  # the core principle
        "bifurcated",  # the bifurcated vision
        "Giles Coefficient",  # archetype reference
        "Pigeon",  # the faculty relationship
        "Binder",  # relationship to the Binder
        "ARCH 301",  # a course
        "ROLE IN THE PRODUCT",  # the product-role flow (folded in)
        "resting in the wrong drawer",  # the signature phrase (folded in)
        "without looking up",  # a voice signature
    ):
        assert beat in p, f"combine dropped: {beat!r}"


def test_compiler_is_deterministic():
    from jeles.persona.compiler import compile_persona

    data = jeles.load_persona()
    assert compile_persona(data) == compile_persona(data)
