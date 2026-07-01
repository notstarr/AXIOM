function cairnApp() {
  const M = window.AxiomModules;
  return Object.assign(
    {},
    M.state.state,
    M.core.methods,
    M.helpers.methods,
    M.projects.methods,
    M.dashboard.methods,
    M.skills.methods,
    M.files.methods,
    M.crawler.methods,
    M.graph.methods,
    M.timeline.methods,
    M.actions.methods,
    M.dispatch.methods,
    M.prompts.methods
  );
}
