//! Sets PyO3's `Py_3_*` cfgs for the target Python, for the few CPython layout differences
//! between versions (`structs.rs`).
fn main() {
    pyo3_build_config::use_pyo3_cfgs();
}
