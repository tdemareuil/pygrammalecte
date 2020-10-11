"""Grammalecte wrapper."""

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, List, Union
from zipfile import ZipFile
import unicodedata
import language_tool_python


import requests


###########################################################
# Message classes
###########################################################

@dataclass
class GrammalecteMessage:
    """Base class for Grammalecte messages."""

    line: int
    start: int
    end: int

    def __str__(self):
        return f"Ligne {self.line} [{self.start}:{self.end}]"

    def __eq__(self, other: "GrammalecteMessage"):
        # to be sortable, but misleading equality usage
        return (self.line, self.start, self.end) == (other.line, other.start, other.end)

    def __lt__(self, other: "GrammalecteMessage"):
        return (self.line, self.start, self.end) < (other.line, other.start, other.end)


@dataclass
class GrammalecteSpellingMessage(GrammalecteMessage):
    """Spelling error message."""

    word: str
    message: str = field(init=False)

    def __post_init__(self):
        self.message = f"Mot inconnu : {self.word}"

    def __str__(self):
        return super().__str__() + " " + self.message

    @staticmethod
    def from_dict(line: int, grammalecte_dict: dict, as_dict=False
        ) -> "GrammalecteSpellingMessage":
        """Instanciate GrammalecteSpellingMessage from Grammalecte result."""
        if as_dict:
            return {
                'line':line,
                'start':int(grammalecte_dict["nStart"]),
                'end':int(grammalecte_dict["nEnd"]),
                'word':grammalecte_dict["sValue"],
            }
        else:
            return GrammalecteSpellingMessage(
                line=line,
                start=int(grammalecte_dict["nStart"]),
                end=int(grammalecte_dict["nEnd"]),
                word=grammalecte_dict["sValue"],
            )


@dataclass
class GrammalecteGrammarMessage(GrammalecteMessage):
    """Grammar error message."""

    url: str
    color: List[int]
    suggestions: List[str]
    message: str
    rule: str
    type: str

    def __str__(self):
        ret = super().__str__() + f" [{self.rule}] {self.message}"
        if self.suggestions:
            ret += f" (Suggestions : {', '.join(self.suggestions)})"
        return ret

    @staticmethod
    def from_dict(line: int, grammalecte_dict: dict, as_dict=False
        ) -> "GrammalecteGrammarMessage":
        """Instanciate GrammalecteGrammarMessage from Grammalecte result."""
        if as_dict:
            return {
                'line':line,
                'start':int(grammalecte_dict["nStart"]),
                'end':int(grammalecte_dict["nEnd"]),
                'url':grammalecte_dict["URL"],
                'color':grammalecte_dict["aColor"],
                'suggestions':grammalecte_dict["aSuggestions"],
                'message':grammalecte_dict["sMessage"],
                'rule':grammalecte_dict["sRuleId"],
                'type':grammalecte_dict["sType"],
            }
        else:
            return GrammalecteGrammarMessage(
                line=line,
                start=int(grammalecte_dict["nStart"]),
                end=int(grammalecte_dict["nEnd"]),
                url=grammalecte_dict["URL"],
                color=grammalecte_dict["aColor"],
                suggestions=grammalecte_dict["aSuggestions"],
                message=grammalecte_dict["sMessage"],
                rule=grammalecte_dict["sRuleId"],
                type=grammalecte_dict["sType"],
            )


###########################################################
# Main functions
###########################################################

def grammalecte_text(text: str, as_dict=False) -> Generator[GrammalecteMessage, None, None]:
    """Run grammalecte on a string, generate messages."""
    
    with tempfile.TemporaryDirectory() as tmpdirname:
        tmpfile = Path(tmpdirname) / "file.txt"
        with open(tmpfile, "w", encoding="utf-8") as f:
            f.write(text)
        yield from grammalecte_file(tmpfile, as_dict=as_dict)


def correct(text: str) -> str:
    
    # get correction messages and sort them in reverse order
    messages = list(grammalecte_text(text, as_dict=True))
    messages = sorted(messages, key = lambda i: (i['line'], i['start']), reverse=True)
    
    # split text in lines and characters
    lines = text.splitlines()
    lines = [list(x) for x in lines]
    
    # apply corrections
    unknown_words = []
    for message in messages:
        line = lines[message['line']-1]
        if 'word' in message:
            unknown_words.append(message['word']) # store unknown words for later
        try:
            line[message['start']:message['end']] = list(message['suggestions'][0])
        except:
            continue
    
    # rebuild text
    corrected_lines = [''.join(x) for x in lines]
    corrected_lines = [l + '.' if not l.endswith(('.', '!', '?', ',', ';', ':')) else l for l in corrected_lines]
    corrected_lines = [l[0].upper() + l[1:] if l[0].upper() != l[0] else l for l in corrected_lines]
    corrected_text = '\n'.join(corrected_lines)
    
    # perform additional checks
    if any(x in corrected_text for x in other_corrections):
        corrected_text = replace_values_in_string(corrected_text, other_corrections)
    for word in unknown_words:
        if word.startswith(('n', 'l', 'd')):
            new_word = word[:1] + "'" + word[1:]
            corrected_text = corrected_text.replace(word, new_word)
    
    # perform languagetool corrections on top of everything
    tool = language_tool_python.LanguageTool('fr')
    corrected_text = tool.correct(corrected_text)
    corrected_text = unicodedata.normalize("NFKD", corrected_text)
    
    return corrected_text


###########################################################
# Helper functions
###########################################################

def grammalecte_file(
    filename: Union[str, Path], as_dict=False
) -> Generator[GrammalecteMessage, None, None]:
    """Run grammalecte on a file given its path, generate messages."""
    stdout = "[]"
    # TODO check existence of a file
    filename = str(filename)
    try:
        result = _run_grammalecte(filename)
        stdout = result.stdout
    except FileNotFoundError as e:
        if e.filename == "grammalecte-cli.py":
            _install_grammalecte()
            result = _run_grammalecte(filename)
            stdout = result.stdout
    yield from _convert_to_messages(stdout, as_dict=as_dict)


def _convert_to_messages(
    grammalecte_json: str, as_dict=False
) -> Generator[GrammalecteMessage, None, None]:
    warnings = json.loads(grammalecte_json)
    for warning in warnings["data"]:
        lineno = int(warning["iParagraph"])
        messages = []
        for error in warning["lGrammarErrors"]:
            messages.append(GrammalecteGrammarMessage.from_dict(lineno, error, as_dict=as_dict))
        for error in warning["lSpellingErrors"]:
            messages.append(GrammalecteSpellingMessage.from_dict(lineno, error, as_dict=as_dict))
        for message in messages:
            yield message


def _run_grammalecte(filepath: str) -> subprocess.CompletedProcess:
    """Run Grammalecte on a file."""
    return subprocess.run(
        [
            "grammalecte-cli.py",
            "-f",
            filepath,
            "-off",
            "apos",
            "--json",
            "--only_when_errors",
        ],
        capture_output=True,
        text=True,
    )


def _install_grammalecte():
    """Install grammalecte CLI."""
    version = "1.11.0"
    tmpdirname = tempfile.mkdtemp(prefix="grammalecte_")
    tmpdirname = Path(tmpdirname)
    tmpdirname.mkdir(exist_ok=True)
    download_request = requests.get(
        f"https://grammalecte.net/grammalecte/zip/Grammalecte-fr-v{version}.zip"
    )
    download_request.raise_for_status()
    zip_file = tmpdirname / f"Grammalecte-fr-v{version}.zip"
    zip_file.write_bytes(download_request.content)
    with ZipFile(zip_file, "r") as zip_obj:
        zip_obj.extractall(tmpdirname / f"Grammalecte-fr-v{version}")
    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            str(tmpdirname / f"Grammalecte-fr-v{version}"),
        ]
    )


###########################################################
# Additional checking dictionary
###########################################################

other_corrections = {
    "!!": "!",
    "??": "?",
    "?!": "?",
    " d ": " d'",
    " n ": " n'",
    " l ": " l'",
    "tps": "temps",
    "ac": "avec",
    "ok": "OK",
    "okay": "OK",
    "pr": "pour",
}

def replace_values_in_string(text, args_dict):
    for key in args_dict.keys():
        text = text.replace(key, str(args_dict[key]))
    return text
