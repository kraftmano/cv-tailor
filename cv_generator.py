"""
cv_generator.py - Generate role-optimised CVs from multiple uploaded CV files.

Takes N input CV .docx files + a target role type string and produces a single
optimised .docx by using the first CV as the structural template and asking
Claude to rewrite bullet points to best represent the target role, drawing on
all uploaded CV content for inspiration.
"""

import json
import re
from pathlib import Path

import anthropic

from cv_tailor import apply_suggestions, create_docx_from_text, extract_cv_paragraphs


def build_role_prompt(role_type: str, template_paragraphs: list[dict], extra_paragraphs: list[dict]) -> str:
    """
    Build a prompt asking Claude to optimise the template CV for a given role type.

    template_paragraphs: paragraphs from the first/base CV (indices are valid for apply_suggestions)
    extra_paragraphs: paragraphs from additional CV versions (for content inspiration only)
    """
    template_list = "\n".join(
        f"[{p['index']}] ({p['style']}) {p['text']}" for p in template_paragraphs
    )

    extra_section = ""
    if extra_paragraphs:
        extra_list = "\n".join(
            f"({p['style']}) {p['text']}" for p in extra_paragraphs
        )
        extra_section = f"""

ADDITIONAL CV CONTENT (for inspiration - do NOT reference these indices):
{extra_list}"""

    return f"""You are an expert CV writer helping tailor a CV for a specific target role.

The candidate is targeting: {role_type}

Below is their BASE CV (the document you will modify) followed by additional content
from other CV versions they have provided.

Your task:
1. Identify up to 10 bullet-point paragraphs in the BASE CV that would benefit from being
   rewritten to better align with a {role_type} role.
2. For each, provide a rewritten version that:
   - Draws on language and content from the ADDITIONAL CV CONTENT where relevant
   - Uses a short bold label (ending with a colon, e.g. "Product Strategy:")
   - Has a concise plain-text description (the achievement, starting with a space)
3. Only rewrite bullet points (lines describing experience/achievements). Do NOT touch:
   - Name, contact details, section headers (Education, Experience, etc.)
   - Job titles, company names, dates
   - Skills or education entries
4. Preserve factual accuracy - do not fabricate metrics or experiences not present in the originals.
5. Keep the tone and length similar to the original bullet points.
6. CRITICAL: Only use paragraph indices from the BASE CV list below.

Return ONLY a JSON array with this exact structure (no markdown, no explanation):
[
  {{
    "index": <paragraph_index_integer from BASE CV>,
    "original_text": "<exact original text for verification>",
    "bold_label": "<Category Label:>",
    "plain_text": " <achievement description>",
    "reason": "<one sentence explaining why this change helps for a {role_type} role>"
  }},
  ...
]

BASE CV PARAGRAPHS:
{template_list}{extra_section}
"""


def get_role_suggestions(
    role_type: str,
    template_paragraphs: list[dict],
    extra_paragraphs: list[dict],
    api_key: str,
) -> list[dict]:
    """Call Claude API and get suggested rewrites for the target role."""
    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_role_prompt(role_type, template_paragraphs, extra_paragraphs)

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    return json.loads(raw)


def safe_filename(role_type: str) -> str:
    """Convert a role type string to a safe filename component."""
    return re.sub(r"[^\w\-]", "_", role_type).strip("_")


def generate_role_cv(
    input_cv_paths: list[str],
    role_type: str,
    output_dir: str,
    api_key: str,
) -> str:
    """
    Generate an optimised CV for a specific role type from multiple input CVs.

    Uses the first CV as the structural template and rewrites bullet points to
    best represent the target role, using all CV content as source material.

    Returns the path to the generated .docx file.
    """
    if not input_cv_paths:
        raise ValueError("At least one input CV path is required")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = str(output_dir / f"role_{safe_filename(role_type)}.docx")

    # Prefer DOCX as template (preserves formatting); fall back to building one from first PDF
    docx_paths = [p for p in input_cv_paths if p.lower().endswith(".docx")]
    pdf_paths  = [p for p in input_cv_paths if p.lower().endswith(".pdf")]

    if docx_paths:
        template_path = docx_paths[0]
        other_paths   = docx_paths[1:] + pdf_paths
    else:
        # PDF-only: build a plain DOCX from the first PDF to act as template
        tmp_docx = str(output_dir / "_template_from_pdf.docx")
        first_pdf_paragraphs = extract_cv_paragraphs(pdf_paths[0])
        create_docx_from_text(first_pdf_paragraphs, tmp_docx)
        template_path = tmp_docx
        other_paths   = pdf_paths[1:]

    # Extract paragraphs from the template CV
    template_paragraphs = extract_cv_paragraphs(template_path)

    # Extract paragraphs from all additional CVs (for content inspiration)
    extra_paragraphs = []
    for path in other_paths:
        try:
            paras = extract_cv_paragraphs(path)
            extra_paragraphs.extend(paras)
        except Exception:
            pass  # Skip unreadable files

    # Ask Claude for role-optimised suggestions
    suggestions = get_role_suggestions(role_type, template_paragraphs, extra_paragraphs, api_key)

    # Apply suggestions to a copy of the template
    apply_suggestions(template_path, output_path, suggestions)

    return output_path


def generate_all_role_cvs(
    input_cv_paths: list[str],
    role_types: list[str],
    output_dir: str,
    api_key: str,
) -> list[dict]:
    """
    Generate one optimised CV per role type from the uploaded CVs.

    Returns a list of dicts: [{"role": "Product Manager", "path": "/path/to/role_Product_Manager.docx"}, ...]
    """
    results = []
    for role in role_types:
        role = role.strip()
        if not role:
            continue
        path = generate_role_cv(input_cv_paths, role, output_dir, api_key)
        results.append({"role": role, "filename": Path(path).name, "path": path})
    return results
