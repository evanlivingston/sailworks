fn main() {
    // PyO3 0.30 leaves macOS extension-module link flags to the crate build
    // script; without them the cdylib must resolve Python symbols at link time.
    pyo3_build_config::add_extension_module_link_args();
}
