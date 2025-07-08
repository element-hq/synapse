use icu_segmenter::options::WordBreakInvariantOptions;
use icu_segmenter::WordSegmenter;
use pyo3::prelude::*;

#[pyfunction]
pub fn parse_words(text: &str) -> PyResult<Vec<String>> {
    let segmenter = WordSegmenter::new_auto(WordBreakInvariantOptions::default());
    let mut parts = Vec::new();
    let mut last = 0usize;

    // `segment_str` gives us word boundaries as a vector of indexes. Use that
    // to build a vector of words, and return.
    for boundary in segmenter.segment_str(text) {
        if boundary > last {
            parts.push(text[last..boundary].to_string());
        }
        last = boundary;
    }
    Ok(parts)
}

pub fn register_module(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let child_module = PyModule::new(py, "segmenter")?;
    child_module.add_function(wrap_pyfunction!(parse_words, m)?)?;

    m.add_submodule(&child_module)?;

    py.import("sys")?
        .getattr("modules")?
        .set_item("synapse.synapse_rust.segmenter", child_module)?;

    Ok(())
}
