"""Shared test fixtures for genealogy agent tests."""

import pytest

from genealogy_agent.gedcom_parser import GedcomTree


# Minimal GEDCOM file for testing
SAMPLE_GEDCOM = """\
0 HEAD
1 SOUR TEST
1 GEDC
2 VERS 5.5.1
0 @I1@ INDI
1 NAME John /Smith/
2 GIVN John
2 SURN Smith
1 SEX M
1 BIRT
2 DATE 15 MAR 1850
2 PLAC Springfield, Illinois
1 DEAT
2 DATE 22 NOV 1920
2 PLAC Chicago, Illinois
1 OCCU Farmer
1 FAMS @F1@
0 @I2@ INDI
1 NAME Mary /Jones/
2 GIVN Mary
2 SURN Jones
1 SEX F
1 BIRT
2 DATE 8 JUN 1855
2 PLAC Springfield, Illinois
1 DEAT
2 DATE 3 APR 1930
2 PLAC Chicago, Illinois
1 FAMS @F1@
0 @I3@ INDI
1 NAME William /Smith/
2 GIVN William
2 SURN Smith
1 SEX M
1 BIRT
2 DATE 12 FEB 1878
2 PLAC Springfield, Illinois
1 DEAT
2 DATE 5 SEP 1945
2 PLAC Denver, Colorado
1 FAMC @F1@
1 FAMS @F2@
0 @I4@ INDI
1 NAME Sarah /Brown/
2 GIVN Sarah
2 SURN Brown
1 SEX F
1 BIRT
2 DATE 20 JUL 1882
2 PLAC Decatur, Illinois
1 FAMC @F3@
1 FAMS @F2@
0 @I5@ INDI
1 NAME James /Smith/
2 GIVN James
2 SURN Smith
1 SEX M
1 BIRT
2 DATE 3 OCT 1905
2 PLAC Denver, Colorado
1 FAMC @F2@
0 @I6@ INDI
1 NAME Elizabeth /Smith/
2 GIVN Elizabeth
2 SURN Smith
1 SEX F
1 BIRT
2 DATE 17 DEC 1908
2 PLAC Denver, Colorado
1 FAMC @F2@
0 @I7@ INDI
1 NAME Robert /Brown/
2 GIVN Robert
2 SURN Brown
1 SEX M
1 BIRT
2 DATE 1 JAN 1860
2 PLAC Decatur, Illinois
1 FAMS @F3@
0 @I8@ INDI
1 NAME Anna /Miller/
2 GIVN Anna
2 SURN Miller
1 SEX F
1 BIRT
2 DATE 15 MAR 1862
2 PLAC Decatur, Illinois
1 FAMS @F3@
0 @F1@ FAM
1 HUSB @I1@
1 WIFE @I2@
1 CHIL @I3@
1 MARR
2 DATE 10 JUN 1876
2 PLAC Springfield, Illinois
0 @F2@ FAM
1 HUSB @I3@
1 WIFE @I4@
1 CHIL @I5@
1 CHIL @I6@
1 MARR
2 DATE 5 MAY 1903
2 PLAC Denver, Colorado
0 @F3@ FAM
1 HUSB @I7@
1 WIFE @I8@
1 CHIL @I4@
0 TRLR
"""


@pytest.fixture
def sample_gedcom_path(tmp_path):
    """Write the sample GEDCOM to a temp file and return its path."""
    path = tmp_path / "test.ged"
    path.write_text(SAMPLE_GEDCOM)
    return str(path)


@pytest.fixture
def tree(sample_gedcom_path):
    """Parse the sample GEDCOM into a GedcomTree."""
    return GedcomTree.from_file(sample_gedcom_path)


@pytest.fixture
def knowledge_db_path(tmp_path):
    """Return a temp path for a knowledge database."""
    return str(tmp_path / "knowledge.db")


@pytest.fixture
def config(sample_gedcom_path, knowledge_db_path):
    """Return a test config dict."""
    return {
        "server": {"host": "localhost", "ws_port": 9765, "web_port": 9766},
        "app": {
            "title": "Test Genealogy",
            "gedcom": sample_gedcom_path,
            "knowledge_db": knowledge_db_path,
        },
        "ollama": {
            "url": "http://localhost:11434",
            "models": {
                "researcher": "llama3.2:3b",
                "fact_checker": "qwen2.5:7b",
                "narrator": "llama3.1:8b",
            },
        },
        "personalities": {"enabled": True},
        "consensus": {
            "enabled": True,
            "timeout": 30,
            "debate_enabled": True,
            "debate_rounds": 2,
            "disagreement_threshold": 0.6,
        },
        "training": {
            "feedback_enabled": True,
            "heuristics_enabled": True,
        },
        "theme": {"primary": "#e94560", "background": "#1a1a2e"},
    }
