"""
CV Tailor - Streamlit GUI
Run with: streamlit run app.py
"""

import os
import re
import time
from pathlib import Path

import streamlit as st

# ── Page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="CV Tailor",
    page_icon="📄",
    layout="centered",
)

st.title("CV Tailor")
st.markdown("Paste a job description, pick your CV template, and get a tailored CV in seconds.")

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"

TEMPLATE_OPTIONS = {
    "Product": "product.docx",
    "Customer Success": "customer_success.docx",
    "Growth": "growth.docx",
    "Chief of Staff": "chief_of_staff.docx",
}

# ── API Key ──────────────────────────────────────────────────────────────────
api_key = os.environ.get("ANTHROPIC_API_KEY", "")
if not api_key:
    with st.sidebar:
        st.header("Settings")
        api_key = st.text_input(
            "Anthropic API Key",
            type="password",
            placeholder="sk-ant-...",
            help="Your Anthropic API key. Get one at console.anthropic.com",
        )
        if api_key:
            st.success("API key set")
        else:
            st.warning("Enter your API key to continue")
else:
    with st.sidebar:
        st.header("Settings")
        st.success("API key loaded from environment")

# ── Sidebar: template status ─────────────────────────────────────────────────
with st.sidebar:
    st.divider()
    st.subheader("CV Templates")
    for label, filename in TEMPLATE_OPTIONS.items():
        path = TEMPLATES_DIR / filename
        if path.exists():
            st.markdown(f"- {label}")
        else:
            st.markdown(f"- ~~{label}~~ *(missing)*")
    st.caption(f"Templates folder: `cv_tailor_app/templates/`")

# ── Main form ────────────────────────────────────────────────────────────────
col1, col2 = st.columns(2)

with col1:
    company = st.text_input("Company name", placeholder="e.g. Decagon")

with col2:
    job_title = st.text_input("Job title", placeholder="e.g. Agent Success Manager")

st.divider()

# CV template selection
st.subheader("1. Choose CV Template")

available = {
    label: filename
    for label, filename in TEMPLATE_OPTIONS.items()
    if (TEMPLATES_DIR / filename).exists()
}

if not available:
    st.error(
        "No CV templates found. Please add .docx files to `cv_tailor_app/templates/`.\n\n"
        "Expected files: product.docx, customer_success.docx, growth.docx, chief_of_staff.docx"
    )
    st.stop()

selected_template_label = st.radio(
    "Select baseline CV",
    options=list(available.keys()),
    horizontal=True,
)
selected_template_path = str(TEMPLATES_DIR / available[selected_template_label])

st.divider()

# JD input
st.subheader("2. Job Description")

jd_tab1, jd_tab2 = st.tabs(["Paste text", "Upload .txt file"])

with jd_tab1:
    jd_text = st.text_area(
        "Paste the job description here",
        height=300,
        placeholder="Paste the full job description...",
        label_visibility="collapsed",
    )

with jd_tab2:
    uploaded_file = st.file_uploader("Upload a .txt file", type=["txt"])
    if uploaded_file:
        jd_text = uploaded_file.read().decode("utf-8")
        st.success(f"Loaded {len(jd_text)} characters from {uploaded_file.name}")

st.divider()

# ── Generate button ───────────────────────────────────────────────────────────
st.subheader("3. Generate Tailored CV")

ready = bool(api_key and jd_text and jd_text.strip() and company and job_title)

if not ready:
    missing = []
    if not api_key:
        missing.append("API key (in sidebar)")
    if not company:
        missing.append("company name")
    if not job_title:
        missing.append("job title")
    if not jd_text or not jd_text.strip():
        missing.append("job description")
    st.info(f"Still needed: {', '.join(missing)}")

generate_btn = st.button(
    "Tailor My CV",
    type="primary",
    disabled=not ready,
    use_container_width=True,
)

if generate_btn:
    # Build output filename
    safe_company = re.sub(r"[^\w\-]", "_", company)
    safe_role = re.sub(r"[^\w\-]", "_", job_title)
    output_stem = f"Oliver_Kraftman_CV_{safe_company}_{safe_role}"

    progress = st.progress(0, text="Starting...")
    status = st.empty()

    try:
        from cv_tailor import tailor_cv

        status.info("Extracting CV paragraphs...")
        progress.progress(10, text="Reading CV template...")
        time.sleep(0.3)

        status.info("Sending to Claude for analysis... (this takes ~15-30 seconds)")
        progress.progress(30, text="Claude is analysing the JD and CV...")

        result = tailor_cv(
            template_path=selected_template_path,
            jd_text=jd_text.strip(),
            output_stem=output_stem,
            output_dir=str(OUTPUT_DIR),
            api_key=api_key,
        )

        progress.progress(80, text="Applying edits and converting to PDF...")
        time.sleep(0.5)
        progress.progress(100, text="Done!")
        status.empty()

        st.success(f"CV tailored successfully — {len(result['applied'])} bullet points rewritten.")

        # ── Download buttons ────────────────────────────────────────────────
        st.subheader("Download Your CV")
        dl_col1, dl_col2 = st.columns(2)

        with dl_col1:
            with open(result["docx_path"], "rb") as f:
                st.download_button(
                    label="Download .docx",
                    data=f.read(),
                    file_name=f"{output_stem}.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    use_container_width=True,
                )

        with dl_col2:
            if Path(result["pdf_path"]).exists():
                with open(result["pdf_path"], "rb") as f:
                    st.download_button(
                        label="Download PDF",
                        data=f.read(),
                        file_name=f"{output_stem}.pdf",
                        mime="application/pdf",
                        use_container_width=True,
                    )
            else:
                st.warning("PDF conversion failed (is Microsoft Word installed?)")

        # ── Change summary ──────────────────────────────────────────────────
        with st.expander(f"View changes ({len(result['applied'])} applied)", expanded=True):
            for change in result["applied"]:
                st.markdown(f"**Para {change['index']}** — {change['new_label']}")
                st.markdown(
                    f"<span style='color:gray;font-size:0.85em;'>Was: {change['original_text'][:100]}...</span>",
                    unsafe_allow_html=True,
                )
                st.markdown(f"New: **{change['new_label']}**{change['new_text']}")
                if change.get("reason"):
                    st.caption(f"Why: {change['reason']}")
                st.divider()

        if result["skipped"]:
            with st.expander(f"Skipped ({len(result['skipped'])} paragraphs)"):
                for s in result["skipped"]:
                    st.markdown(f"- Para {s['index']}: {s.get('reason', 'unknown')}")

    except Exception as e:
        progress.empty()
        status.empty()
        st.error(f"Error: {e}")
        st.exception(e)
