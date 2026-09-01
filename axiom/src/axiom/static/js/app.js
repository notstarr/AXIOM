function axiomApp() {
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
    M.mcp.methods,
    M.graph.methods,
    M.human.methods,
    M.archives.methods,
    M.timeline.methods,
    M.actions.methods,
    M.dispatch.methods,
    M.prompts.methods,
    M.workerTools.methods,
    M.storage.methods
  );
}
