const { createApp, ref, reactive, computed, watch, nextTick, onMounted } = Vue;

const API = '';

async function api(path, opts) {
  const res = await fetch(API + path, opts);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

createApp({
  setup() {
    const site = ref('');
    const component = ref('');
    const sites = ref([]);
    const components = ref([]);
    const tab = ref('generate');
    const settingsTab = ref('component');
    const jobsOpen = ref(true);
    const jobs = ref([]);
    const showRunTroubleshoot = ref(false);

    const tabs = [
      { id: 'discover', label: 'Connect Target' },
      { id: 'generate', label: 'Generate Tests' },
      { id: 'tests', label: 'Test Management' },
      { id: 'run', label: 'Run Tests' },
      { id: 'risk', label: 'Risk Assessment' },
      { id: 'export', label: 'Export' },
      { id: 'settings', label: 'Settings' },
    ];

    const allStrategies = ref([]);
    const allFrameworks = ref([]);
    const runStrategies = ref([]);
    const runFrameworks = ref([]);
    const runAllFrameworks = ref([]);
    const logs = reactive({ runs: [], compliance: [], reports: [] });

    // --- Test Management tab ---
    const tmStrategy = ref('');
    const tmFramework = ref('');
    const tmStrategies = ref([]);
    const tmFrameworks = ref([]);
    const tmFile = ref(null);       // loaded test file { framework, description, mandates }
    const tmDirty = ref(false);
    const tmSaving = ref(false);
    const tmSaveMsg = ref('');
    const tmEditingId = ref(null);  // prompt id being inline-edited
    const tmAddingMandate = ref('');// mandate slug for new-prompt form
    const tmNewPrompt = reactive({ id: '', description: '', prompt: '' });
    const tmImportFile = ref(null);
    const tmImportName = ref('');
    const tmImporting = ref(false);
    const tmImportMsg = ref('');

    async function tmLoadStrategies() {
      tmStrategies.value = [];
      tmStrategy.value = '';
      tmFrameworks.value = [];
      tmFramework.value = '';
      tmFile.value = null;
      if (!site.value || !component.value) return;
      const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
      tmStrategies.value = await api(`/api/sites/${s}/${c}/strategies`);
    }

    async function tmLoadFrameworks() {
      tmFrameworks.value = [];
      tmFramework.value = '';
      tmFile.value = null;
      tmDirty.value = false;
      if (tmStrategy.value && site.value && component.value) {
        const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
        tmFrameworks.value = await api(`/api/sites/${s}/${c}/strategies/${encodeURIComponent(tmStrategy.value)}/frameworks`);
      }
    }

    async function tmLoadFile() {
      tmFile.value = null;
      tmDirty.value = false;
      tmEditingId.value = null;
      tmAddingMandate.value = '';
      if (!tmFramework.value || !tmStrategy.value) return;
      const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
      const fw = encodeURIComponent(tmFramework.value);
      const strat = encodeURIComponent(tmStrategy.value);
      // tmFramework.value holds the full path; extract stem from it
      const stem = tmFramework.value.split('/').pop().replace(/\.json$/, '');
      tmFile.value = await api(`/api/sites/${s}/${c}/tests/${strat}/${encodeURIComponent(stem)}`);
    }

    function tmSnapshotPlain() {
      return JSON.parse(JSON.stringify(tmFile.value));
    }

    async function tmSave() {
      if (!tmFile.value) return;
      tmSaving.value = true;
      tmSaveMsg.value = '';
      try {
        const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
        const strat = encodeURIComponent(tmStrategy.value);
        const stem = tmFramework.value.split('/').pop().replace(/\.json$/, '');
        await api(`/api/sites/${s}/${c}/tests/${strat}/${encodeURIComponent(stem)}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data: tmSnapshotPlain() }),
        });
        tmDirty.value = false;
        tmSaveMsg.value = 'Saved';
        setTimeout(() => { tmSaveMsg.value = ''; }, 2000);
      } catch (e) {
        tmSaveMsg.value = 'Save failed: ' + e.message;
      } finally {
        tmSaving.value = false;
      }
    }

    async function tmDeletePrompt(mandateIdx, promptIdx) {
      const m = tmFile.value.mandates[mandateIdx];
      if (!m?.prompts?.length) return;
      if (promptIdx < 0 || promptIdx >= m.prompts.length) return;
      m.prompts = m.prompts.filter((_, i) => i !== promptIdx);
      tmDirty.value = true;
      await tmSave();
    }

    function tmStartAdd(mandateSlug) {
      tmAddingMandate.value = mandateSlug;
      tmNewPrompt.id = '';
      tmNewPrompt.description = '';
      tmNewPrompt.prompt = '';
    }

    function tmConfirmAdd(mandateIdx) {
      const p = { id: tmNewPrompt.id.trim(), description: tmNewPrompt.description.trim(), prompt: tmNewPrompt.prompt.trim() };
      if (!p.id || !p.prompt) return;
      tmFile.value.mandates[mandateIdx].prompts.push(p);
      tmDirty.value = true;
      tmAddingMandate.value = '';
    }

    function tmMarkDirty() { tmDirty.value = true; }

    function tmImportFileChanged(event) {
      const file = event.target.files?.[0] || null;
      tmImportFile.value = file;
      tmImportMsg.value = '';
      if (file && !tmImportName.value) {
        tmImportName.value = file.name.replace(/\.json$/i, '');
      }
    }

    function tmReadImportFile(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
          try {
            resolve(JSON.parse(reader.result));
          } catch (e) {
            reject(new Error('Invalid JSON: ' + e.message));
          }
        };
        reader.onerror = () => reject(new Error('Could not read file'));
        reader.readAsText(file);
      });
    }

    async function tmImportZeroShot() {
      if (!site.value || !component.value || !tmImportFile.value) return;
      if (tmDirty.value && !confirm('Discard unsaved test edits and open the imported file?')) return;
      tmImporting.value = true;
      tmImportMsg.value = '';
      try {
        const data = await tmReadImportFile(tmImportFile.value);
        const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
        const result = await api(`/api/sites/${s}/${c}/tests/import-zero-shot`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: tmImportName.value || tmImportFile.value.name, data }),
        });
        await tmLoadStrategies();
        tmStrategy.value = result.strategy;
        await tmLoadFrameworks();
        tmFramework.value = result.path;
        await tmLoadFile();
        tmImportMsg.value = `Imported ${result.framework} into Zero-shot`;
      } catch (e) {
        tmImportMsg.value = 'Import failed: ' + e.message;
      } finally {
        tmImporting.value = false;
      }
    }

    const gen = reactive({ strategy: 'zero_shot', framework: 'eu_ai_act' });
    const run = reactive({ strategy: '', framework: '', assess: false });
    const risk = reactive({ log: '' });
    const exp = reactive({ report: '', program_id: '' });
    const expResult = ref(null);
    const expPreview = ref(null);
    // host + api_key stored server-side in .env; program_id is per-export
    const expCreds = reactive({ host: '', has_api_key: false });
    const expCredsEdit = reactive({ host: '', api_key: '' });
    const expCredsSaving = ref(false);
    const expCredsMsg = ref('');

    async function loadExpCreds() {
      try {
        const c = await api('/api/credentials');
        expCreds.host = c.host || '';
        expCreds.has_api_key = c.has_api_key || false;
        expCredsEdit.host = c.host || '';
        expCredsEdit.api_key = '';
        // Pre-fill program_id from .env if not already set by the user
        if (c.program_id && !exp.program_id) exp.program_id = c.program_id;
      } catch { /* ignore */ }
    }

    async function saveExpCreds() {
      expCredsSaving.value = true;
      expCredsMsg.value = '';
      try {
        const result = await api('/api/credentials', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host: expCredsEdit.host, api_key: expCredsEdit.api_key }),
        });
        expCreds.host = result.host;
        expCreds.has_api_key = result.has_api_key;
        expCredsEdit.api_key = '';
        expCredsMsg.value = 'Saved to .env';
      } catch (e) {
        expCredsMsg.value = 'Save failed: ' + e.message;
      } finally {
        expCredsSaving.value = false;
      }
    }

    async function clearExpCreds() {
      if (!confirm('Remove AIRTA Systems host and API key from .env?')) return;
      await api('/api/credentials', { method: 'DELETE' });
      expCreds.host = '';
      expCreds.has_api_key = false;
      expCredsEdit.host = expCredsEdit.api_key = '';
      expCredsMsg.value = 'Credentials cleared';
    }

    watch(() => exp.report, async (path) => {
      expPreview.value = null;
      expResult.value = null;
      if (!path) return;
      try {
        const data = await api(`/api/log?path=${encodeURIComponent(path)}`);
        expPreview.value = {
          count: (data.adversarial_results || []).length,
          framework: data.framework || '',
          timestamp: data.timestamp || '',
        };
      } catch { /* ignore */ }
    });
    const cache = reactive({ deleteOnServer: false });

    // Component config
    const INPUT_TYPES = ['text', 'textarea', 'contenteditable', 'password', 'email', 'search', 'select', 'combobox', 'checkbox', 'radio'];
    const compCfg = reactive({
      login_url: '',
      submission: { start_url: '', inputs: [], submit_selector: '', response_selector: '', submit_via: 'click', response_wait_ms: 5000 },
    });
    const compCfgSaved = ref(false);
    const compCfgError = ref('');
    const compCfgEmpty = ref(false);

    async function loadCompCfg() {
      if (!site.value || !component.value) return;
      compCfgError.value = '';
      try {
        const data = await api(`/api/sites/${encodeURIComponent(site.value)}/${encodeURIComponent(component.value)}/config`);
        compCfg.login_url = data.login_url || '';
        const sub = data.submission || {};
        compCfg.submission.start_url = sub.start_url || '';
        compCfg.submission.submit_selector = sub.submit_selector || '';
        compCfg.submission.response_selector = sub.response_selector || '';
        compCfg.submission.submit_via = sub.submit_via || 'click';
        compCfg.submission.response_wait_ms = sub.response_wait_ms ?? 5000;
        compCfg.submission.inputs = (sub.inputs || []).map(inp => ({ ...inp }));
        compCfgEmpty.value = !data.submission;
      } catch (e) { compCfgError.value = String(e); }
    }

    async function saveCompCfg() {
      compCfgError.value = '';
      compCfgSaved.value = false;
      try {
        const payload = {
          login_url: compCfg.login_url,
          submission: {
            start_url: compCfg.submission.start_url,
            inputs: compCfg.submission.inputs.map(i => ({ ...i })),
            submit_selector: compCfg.submission.submit_selector,
            response_selector: compCfg.submission.response_selector,
            submit_via: compCfg.submission.submit_via,
            response_wait_ms: Number(compCfg.submission.response_wait_ms),
          },
        };
        await api(`/api/sites/${encodeURIComponent(site.value)}/${encodeURIComponent(component.value)}/config`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ config: payload }),
        });
        compCfgSaved.value = true;
        setTimeout(() => { compCfgSaved.value = false; }, 3000);
        await loadContext();
      } catch (e) { compCfgError.value = String(e); }
    }

    function addInput() {
      compCfg.submission.inputs.push({ selector: '', type: 'text' });
    }
    function removeInput(i) {
      compCfg.submission.inputs.splice(i, 1);
    }

    // Rubrics
    const companyRubricText = ref('');
    const companySaved = ref(false);
    const companyError = ref('');
    const companyGenerating = ref(false);
    const companyGenerateUrl = ref('');
    const componentRubricText = ref('');
    const componentRubricSaved = ref(false);
    const componentRubricError = ref('');
    const componentRubricGenerating = ref(false);
    const componentGenerateUrl = ref('');

    async function loadRubrics() {
      if (!site.value) return;
      companyError.value = '';
      componentRubricError.value = '';
      try {
        const data = await api(`/api/sites/${encodeURIComponent(site.value)}/company-rubric`);
        companyRubricText.value = JSON.stringify(data, null, 2);
      } catch { companyRubricText.value = '{}'; }
      if (component.value) {
        try {
          const data = await api(`/api/sites/${encodeURIComponent(site.value)}/${encodeURIComponent(component.value)}/component-rubric`);
          componentRubricText.value = JSON.stringify(data, null, 2);
        } catch { componentRubricText.value = '{}'; }
        // Pre-fill generate URL with component start URL if available
        if (!componentGenerateUrl.value) {
          componentGenerateUrl.value = `https://${site.value}`;
        }
      } else {
        componentRubricText.value = '';
      }
      if (!companyGenerateUrl.value && site.value) {
        companyGenerateUrl.value = `https://${site.value}/about`;
      }
    }

    async function saveCompanyRubric() {
      companyError.value = '';
      companySaved.value = false;
      try {
        const content = JSON.parse(companyRubricText.value);
        await api(`/api/sites/${encodeURIComponent(site.value)}/company-rubric`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content }),
        });
        companySaved.value = true;
        setTimeout(() => { companySaved.value = false; }, 3000);
      } catch (e) { companyError.value = String(e); }
    }

    async function generateCompanyRubric() {
      if (!companyGenerateUrl.value || !site.value) return;
      companyError.value = '';
      companySaved.value = false;
      companyGenerating.value = true;
      try {
        const res = await api(`/api/sites/${encodeURIComponent(site.value)}/company-rubric/generate`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: companyGenerateUrl.value }),
        });
        companyRubricText.value = JSON.stringify(res.content, null, 2);
        companySaved.value = true;
        setTimeout(() => { companySaved.value = false; }, 4000);
      } catch (e) { companyError.value = String(e); }
      finally { companyGenerating.value = false; }
    }

    async function saveComponentRubric() {
      componentRubricError.value = '';
      componentRubricSaved.value = false;
      try {
        const content = JSON.parse(componentRubricText.value);
        await api(`/api/sites/${encodeURIComponent(site.value)}/${encodeURIComponent(component.value)}/component-rubric`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content }),
        });
        componentRubricSaved.value = true;
        setTimeout(() => { componentRubricSaved.value = false; }, 3000);
      } catch (e) { componentRubricError.value = String(e); }
    }

    async function generateComponentRubric() {
      if (!componentGenerateUrl.value || !site.value || !component.value) return;
      componentRubricError.value = '';
      componentRubricSaved.value = false;
      componentRubricGenerating.value = true;
      try {
        const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
        const res = await api(`/api/sites/${s}/${c}/component-rubric/generate`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: componentGenerateUrl.value }),
        });
        componentRubricText.value = JSON.stringify(res.content, null, 2);
        componentRubricSaved.value = true;
        setTimeout(() => { componentRubricSaved.value = false; }, 4000);
      } catch (e) { componentRubricError.value = String(e); }
      finally { componentRubricGenerating.value = false; }
    }

    const cfg = reactive({});
    const cfgSaved = ref(false);
    const cfgError = ref('');
    const BLOCKED_OPTIONS = ['image', 'font', 'media', 'stylesheet'];
    const COUNTRIES = ['US', 'UK', 'DE', 'FR', 'JP', 'CA', 'AU', 'NL', 'ES', 'IT'];
    const CHANNELS = ['chromium', 'chrome', 'chrome-beta', 'msedge'];
    const FETCH_METHODS = ['auto', 'pool', 'cluster', 'human'];

    async function loadConfig() {
      try {
        const data = await api('/api/config');
        Object.assign(cfg, data);
        // Ensure BLOCKED_TYPES is always an array for checkbox binding
        if (!Array.isArray(cfg.BLOCKED_TYPES)) cfg.BLOCKED_TYPES = [];
      } catch (e) {
        cfgError.value = String(e);
      }
    }

    async function saveConfig() {
      cfgError.value = '';
      cfgSaved.value = false;
      try {
        await api('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ changes: { ...cfg } }),
        });
        cfgSaved.value = true;
        setTimeout(() => { cfgSaved.value = false; }, 3000);
      } catch (e) {
        cfgError.value = String(e);
      }
    }

    function toggleBlocked(type) {
      const idx = cfg.BLOCKED_TYPES.indexOf(type);
      if (idx === -1) cfg.BLOCKED_TYPES.push(type);
      else cfg.BLOCKED_TYPES.splice(idx, 1);
    }

    // --- Startup modal ---
    const showModal = ref(false);
    const modalSite = ref('');
    const modalComponent = ref('');
    const modalComponents = ref([]);
    const modalNewSite = ref('');
    const modalNewComponent = ref('');
    const modalRenameSite = ref('');
    const modalRenameComponent = ref('');
    const modalError = ref('');
    const modalMsg = ref('');

    async function onModalSiteChange() {
      modalComponent.value = '';
      modalComponents.value = [];
      modalNewSite.value = '';
      modalNewComponent.value = '';
      modalRenameSite.value = modalSite.value || '';
      modalRenameComponent.value = '';
      if (modalSite.value) {
        modalComponents.value = await api(`/api/sites/${encodeURIComponent(modalSite.value)}/components`);
      }
    }

    function onModalComponentChange() {
      modalRenameComponent.value = modalComponent.value || '';
    }

    async function modalCreateSite() {
      modalError.value = '';
      modalMsg.value = '';
      const domain = modalNewSite.value.trim();
      if (!domain) { modalError.value = 'Enter a domain.'; return; }
      try {
        const created = await api('/api/sites', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ domain }),
        });
        await loadSites();
        modalSite.value = created.domain;
        modalRenameSite.value = created.domain;
        modalNewSite.value = '';
        modalComponent.value = '';
        modalRenameComponent.value = '';
        modalComponents.value = await api(`/api/sites/${encodeURIComponent(created.domain)}/components`);
        modalMsg.value = 'Site created';
      } catch (e) {
        modalError.value = 'Create site failed: ' + e.message;
      }
    }

    async function modalRenameSiteAction() {
      modalError.value = '';
      modalMsg.value = '';
      const current = modalSite.value;
      const next = modalRenameSite.value.trim();
      if (!current || !next || current === next) return;
      try {
        const renamed = await api(`/api/sites/${encodeURIComponent(current)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ domain: next }),
        });
        if (site.value === current) site.value = renamed.domain;
        await loadSites();
        modalSite.value = renamed.domain;
        modalRenameSite.value = renamed.domain;
        modalComponents.value = await api(`/api/sites/${encodeURIComponent(renamed.domain)}/components`);
        components.value = site.value === renamed.domain ? [...modalComponents.value] : components.value;
        modalMsg.value = 'Site renamed';
      } catch (e) {
        modalError.value = 'Rename site failed: ' + e.message;
      }
    }

    async function modalDeleteSite() {
      modalError.value = '';
      modalMsg.value = '';
      if (!modalSite.value) return;
      if (!confirm(`Delete site "${modalSite.value}" and all components?`)) return;
      const deleting = modalSite.value;
      try {
        await api(`/api/sites/${encodeURIComponent(deleting)}`, { method: 'DELETE' });
        if (site.value === deleting) {
          site.value = '';
          component.value = '';
          components.value = [];
        }
        await loadSites();
        modalSite.value = '';
        modalRenameSite.value = '';
        modalComponent.value = '';
        modalRenameComponent.value = '';
        modalComponents.value = [];
        modalMsg.value = 'Site deleted';
      } catch (e) {
        modalError.value = 'Delete site failed: ' + e.message;
      }
    }

    async function modalCreateComponent() {
      modalError.value = '';
      modalMsg.value = '';
      if (!modalSite.value) { modalError.value = 'Select a site first.'; return; }
      const name = modalNewComponent.value.trim();
      if (!name) { modalError.value = 'Enter a component name.'; return; }
      try {
        const created = await api(`/api/sites/${encodeURIComponent(modalSite.value)}/components`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
        modalComponents.value = await api(`/api/sites/${encodeURIComponent(modalSite.value)}/components`);
        if (site.value === modalSite.value) components.value = [...modalComponents.value];
        modalComponent.value = created.name;
        modalRenameComponent.value = created.name;
        modalNewComponent.value = '';
        modalMsg.value = 'Component created';
      } catch (e) {
        modalError.value = 'Create component failed: ' + e.message;
      }
    }

    async function modalRenameComponentAction() {
      modalError.value = '';
      modalMsg.value = '';
      const current = modalComponent.value;
      const next = modalRenameComponent.value.trim();
      if (!modalSite.value || !current || !next || current === next) return;
      try {
        const renamed = await api(`/api/sites/${encodeURIComponent(modalSite.value)}/components/${encodeURIComponent(current)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: next }),
        });
        if (site.value === modalSite.value && component.value === current) component.value = renamed.name;
        modalComponents.value = await api(`/api/sites/${encodeURIComponent(modalSite.value)}/components`);
        if (site.value === modalSite.value) components.value = [...modalComponents.value];
        modalComponent.value = renamed.name;
        modalRenameComponent.value = renamed.name;
        modalMsg.value = 'Component renamed';
      } catch (e) {
        modalError.value = 'Rename component failed: ' + e.message;
      }
    }

    async function modalDeleteComponent() {
      modalError.value = '';
      modalMsg.value = '';
      if (!modalSite.value || !modalComponent.value) return;
      if (!confirm(`Delete component "${modalComponent.value}"?`)) return;
      const deleting = modalComponent.value;
      try {
        await api(`/api/sites/${encodeURIComponent(modalSite.value)}/components/${encodeURIComponent(deleting)}`, { method: 'DELETE' });
        if (site.value === modalSite.value && component.value === deleting) component.value = '';
        modalComponents.value = await api(`/api/sites/${encodeURIComponent(modalSite.value)}/components`);
        if (site.value === modalSite.value) components.value = [...modalComponents.value];
        modalComponent.value = '';
        modalRenameComponent.value = '';
        modalMsg.value = 'Component deleted';
      } catch (e) {
        modalError.value = 'Delete component failed: ' + e.message;
      }
    }

    async function confirmModal() {
      modalError.value = '';
      let s = modalSite.value, c = modalComponent.value;
      if (!s) { modalError.value = 'Select or create a site.'; return; }
      if (!c) { modalError.value = 'Select or create a component.'; return; }
      site.value = s;
      components.value = await api(`/api/sites/${encodeURIComponent(s)}/components`);
      component.value = c;
      await loadContext();
      showModal.value = false;
      await checkSetupAndNavigate();
    }

    // --- Onboarding hints ---
    const HINTS = {
      generate: {
        title: 'Generate Tests',
        text: 'Create test suites by choosing a strategy (e.g. Zero-shot) and framework (e.g. EU AI Act). Tests are generated via LLM and saved to the component\'s tests/ directory, ready to run.',
      },
      discover: {
        title: 'Browser Discovery',
        text: 'Records how browser-bot interacts with the target UI. A real browser opens and walks through each recording step. Recorded selectors are saved automatically to the component config.',
      },
      run: {
        title: 'Run Tests',
        text: 'Submits each test prompt to the target UI using the configured browser tier. Select a strategy then a framework from your generated tests and click Run. Results appear in the table below and are saved to a timestamped log directory.',
      },
      tests: {
        title: 'Test Management',
        text: 'Open a generated test file by strategy, then edit mandates and prompts in place - add rows, refine wording, or remove items. Save writes changes back to the component\'s tests/ directory for the next run.',
      },
      risk: {
        title: 'Risk Assessment',
        text: 'Runs each entry in a compliance log through an AI judge to determine risk level (compliant → informational → low → medium → high → critical). Select a compliance log from a previous test run. Results are saved as a pipeline_report.json.',
      },
      export: {
        title: 'Export to AIRTA Systems',
        text: 'Sends a pipeline report to a AIRTA Systems instance via the bulk-import API. Select a report, enter your host, API key, and program ID. Each adversarial result is submitted as a finding.',
      },
      cache: {
        title: 'Clear Gemini Cache',
        text: 'Clears the local Gemini API response cache used by the test generator and risk-level agent. Use this to force fresh LLM responses. "Delete server-side" also removes cached content from Google\'s servers.',
      },
      component: {
        title: 'Component Config',
        text: 'Configures how browser-bot interacts with this component\'s UI — the page URL, input selector, submit button, and where to read the AI response from. If no config exists yet, use Discovery to auto-record these selectors.',
      },
      config: {
        title: 'Global Config',
        text: 'Controls browser-bot\'s global behaviour. Changes are written directly to config.py and take effect on the next test run. Key settings: Fetch Method selects the browser tier, Pool/Cluster enhancements add stealth, and Evasion controls retry behaviour on rate limits.',
      },
    };

    const _storedHints = JSON.parse(localStorage.getItem('airta_hints_dismissed') || '{}');
    const hintDismissed = ref({ ..._storedHints });

    function dismissHint(key) {
      hintDismissed.value = { ...hintDismissed.value, [key]: true };
      localStorage.setItem('airta_hints_dismissed', JSON.stringify(hintDismissed.value));
    }

    const runResults = ref([]);
    const runResultsLoading = ref(false);
    const expandedRunRows = ref({});

    async function loadLatestRunLog() {
      if (!site.value || !component.value) return;
      runResultsLoading.value = true;
      try {
        const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
        const l = await api(`/api/sites/${s}/${c}/logs`);
        logs.runs = l.runs; logs.compliance = l.compliance; logs.reports = l.reports;
        if (!l.runs.length) return;
        const data = await api(`/api/files?path=${encodeURIComponent(l.runs[0].path)}`);
        expandedRunRows.value = {};
        if (data.mode === 'multi') {
          runResults.value = (data.batches || []).flatMap(b =>
            (b.turns || []).map((t, ti) => ({
              label: `Batch ${b.batch_index + 1} / Turn ${ti + 1}`,
              input: t.input, response: t.response,
            }))
          );
        } else {
          runResults.value = (data.entries || []).map((e, i) => ({
            label: `#${i + 1}`, input: e.input, response: e.response,
          }));
        }
      } catch (e) { console.error(e); }
      finally { runResultsLoading.value = false; }
    }

    function toggleRunRow(i) {
      expandedRunRows.value = { ...expandedRunRows.value, [i]: !expandedRunRows.value[i] };
    }

    const activeJobs = reactive({});
    const sseConnections = {};
    const runProgress = ref(null);

    function formatRunEta(sec) {
      if (sec == null || sec === '' || Number.isNaN(Number(sec))) return '—';
      const n = Number(sec);
      if (n <= 0) return '~0s';
      if (n < 90) return `~${Math.round(n)}s`;
      const m = Math.floor(n / 60);
      const s = Math.round(n % 60);
      return `~${m}m ${s}s`;
    }

    const runProgressBarLabel = computed(() => {
      const p = runProgress.value;
      if (!p) return '';
      if (p.phase === 'risk' || p.type === 'risk_start' || p.type === 'risk_progress' || p.type === 'risk_done') {
        if (p.type === 'risk_start') return 'Risk assessment…';
        if (p.type === 'risk_done') return 'Risk assessment complete';
        return `Risk assessment · ${p.current ?? 0} / ${p.total ?? 0} entries`;
      }
      if (p.type === 'suite') {
        return `Strategy ${p.current} / ${p.total}${p.strategy ? ' · ' + p.strategy : ''}`;
      }
      if (p.type === 'run_start') return 'Starting tests…';
      if (p.type === 'run_done') return 'Tests complete';
      return `${p.mode === 'multi' ? 'Multi-turn' : 'Single'} · ${p.current ?? 0} / ${p.total ?? 0} prompts`;
    });

    const runProgressEtaText = computed(() => {
      const p = runProgress.value;
      if (!p) return '';
      if (p.type === 'risk_start') return 'Estimating…';
      if (p.phase === 'risk' || p.type === 'risk_progress' || p.type === 'risk_done') {
        if (p.type === 'risk_done') return `${formatRunEta(p.elapsed_sec)} total`;
        if (p.eta_sec != null && p.eta_sec !== '') return `ETA ${formatRunEta(p.eta_sec)} · ${formatRunEta(p.elapsed_sec)} elapsed`;
        return '—';
      }
      if (p.type === 'run_start' || p.type === 'suite') return 'Estimating…';
      if (p.type === 'run_done') return `${formatRunEta(p.elapsed_sec)} total`;
      if (p.eta_sec != null && p.eta_sec !== '') return `ETA ${formatRunEta(p.eta_sec)} · ${formatRunEta(p.elapsed_sec)} elapsed`;
      return '—';
    });

    /** Risk tab: standalone risk job, or run_tests job while in risk phase (e.g. after tests when “assess after” is on). */
    const riskTabProgressBarVisible = computed(() => {
      const p = runProgress.value;
      if (!p) return false;
      if (activeJobs.risk_assess) return true;
      return !!(p.phase === 'risk' && activeJobs.run_tests);
    });

    function pretty(slug) {
      const short = new Set(['eu','ai','uk','us','oecd','gdpr','iso']);
      return (slug || '').replace(/_/g, '-').split('-').filter(Boolean).map(p =>
        short.has(p.toLowerCase()) ? p.toUpperCase() : p.charAt(0).toUpperCase() + p.slice(1)
      ).join(' ');
    }

    function lineClass(line) {
      const t = line.trimStart();
      if (t.startsWith('[evasion]')) return 'line-evasion';
      if (line.startsWith('[+]') || line.startsWith('[*]')) return 'line-ok';
      if (line.startsWith('[!]') || line.startsWith('[-]') || line.startsWith('[error]')) return 'line-err';
      if (line.startsWith('  ')) return 'line-info';
      return '';
    }

    async function loadSites() {
      sites.value = await api('/api/sites');
      allStrategies.value = await api('/api/strategies');
      allFrameworks.value = await api('/api/frameworks');
    }

    async function onSiteChange() {
      component.value = '';
      loginUrl.value = '';
      authConfigured.value = false;
      if (site.value) {
        components.value = await api(`/api/sites/${encodeURIComponent(site.value)}/components`);
        await loadAuthStatus();
      } else {
        components.value = [];
      }
    }

    async function loadContext() {
      if (site.value && component.value) {
        const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
        runStrategies.value = await api(`/api/sites/${s}/${c}/strategies`);
        const l = await api(`/api/sites/${s}/${c}/logs`);
        logs.runs = l.runs; logs.compliance = l.compliance; logs.reports = l.reports;
        if (tab.value === 'settings' && settingsTab.value === 'component') loadCompCfg();
        if (tab.value === 'settings' && settingsTab.value === 'rubrics') loadRubrics();
        if (tab.value === 'tests') await tmLoadStrategies();
      }
    }

    async function loadRunFrameworks() {
      runFrameworks.value = [];
      runAllFrameworks.value = [];
      run.framework = '';
      if (!run.strategy || !site.value || !component.value) return;
      const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
      if (run.strategy === '__all__') {
        runAllFrameworks.value = await api(`/api/sites/${s}/${c}/all-frameworks`);
      } else {
        runFrameworks.value = await api(`/api/sites/${s}/${c}/strategies/${encodeURIComponent(run.strategy)}/frameworks`);
      }
    }

    async function refreshRunTests() {
      if (!site.value || !component.value) return;
      const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
      const prevStrategy = run.strategy;
      runStrategies.value = await api(`/api/sites/${s}/${c}/strategies`);
      // Keep current strategy selection if it still exists after refresh
      if (prevStrategy && runStrategies.value.some(x => x.slug === prevStrategy)) {
        run.strategy = prevStrategy;
        await loadRunFrameworks();
      } else {
        run.strategy = '';
        run.framework = '';
        runFrameworks.value = [];
        runAllFrameworks.value = [];
      }
    }

    // Lines to suppress in the run_tests console — individual prompt/response
    // entries are shown in the results table instead.
    function _isRunDetailLine(line) {
      const t = line.trimStart();
      return t.startsWith('Input: ') || t.startsWith('Response: ') || t.startsWith('Response:None');
    }

    function _isProgressMetaLine(line) {
      return line.trimStart().startsWith('[airta_progress]');
    }

    function activeOutput(type) {
      const jid = activeJobs[type];
      if (!jid) return [];
      const j = jobs.value.find(x => x.id === jid);
      if (!j) return [];
      const lines = j._output || [];
      if (type === 'run_tests' || type === 'risk_assess') {
        return lines.filter(l => !_isRunDetailLine(l) && !_isProgressMetaLine(l));
      }
      return lines;
    }

    function connectSSE(jobId) {
      if (sseConnections[jobId]) return;
      const j = jobs.value.find(x => x.id === jobId);
      if (!j) return;
      if (!j._output) j._output = [];
      const src = new EventSource(`${API}/api/jobs/${jobId}/stream`);
      sseConnections[jobId] = src;
      src.onmessage = (e) => {
        const line = e.data;
        if (line.startsWith('[airta_progress] ')) {
          try {
            const p = JSON.parse(line.slice('[airta_progress] '.length));
            const isRunJob = j.type === 'run_tests' && activeJobs.run_tests === j.id;
            const isRiskJob = j.type === 'risk_assess' && activeJobs.risk_assess === j.id;
            if (isRunJob || isRiskJob) {
              let pct = 0;
              let phase = p.phase || 'submit';
              if (p.type === 'suite') {
                const total = p.total || 0;
                const cur = p.current || 0;
                pct = total ? Math.min(100, Math.round((cur / total) * 100)) : 0;
                phase = 'suite';
              } else if (p.type === 'run_start') {
                pct = 0;
                phase = 'submit';
              } else if (p.type === 'progress' && p.mode) {
                const total = p.total || 0;
                const cur = p.current || 0;
                pct = total ? Math.min(100, Math.round((cur / total) * 100)) : 0;
                phase = 'submit';
              } else if (p.type === 'run_done') {
                pct = 100;
                phase = 'submit';
              } else if (p.type === 'risk_start') {
                pct = 0;
                phase = 'risk';
              } else if (p.type === 'risk_progress') {
                const total = p.total || 0;
                const cur = p.current || 0;
                pct = total ? Math.min(100, Math.round((cur / total) * 100)) : 0;
                phase = 'risk';
              } else if (p.type === 'risk_done') {
                pct = 100;
                phase = 'risk';
              }
              runProgress.value = { ...p, pct, phase };
            }
          } catch { /* ignore */ }
        }
        j._output.push(line);
        nextTick(() => {
          document.querySelectorAll('.console').forEach(el => { el.scrollTop = el.scrollHeight; });
        });
      };
      src.addEventListener('done', (e) => {
        j.status = e.data || 'done';
        src.close();
        delete sseConnections[jobId];
        refreshJobs();
        if (j.type === 'run_tests') {
          loadLatestRunLog();
          setTimeout(() => {
            if (activeJobs.run_tests === j.id) runProgress.value = null;
          }, 5000);
        }
        if (j.type === 'risk_assess') {
          setTimeout(() => {
            if (activeJobs.risk_assess === j.id) runProgress.value = null;
          }, 5000);
        }
      });
      src.onerror = () => {
        src.close();
        delete sseConnections[jobId];
      };
    }

    async function refreshJobs() {
      const list = await api('/api/jobs');
      for (const j of list) {
        const existing = jobs.value.find(x => x.id === j.id);
        if (existing) {
          existing.status = j.status;
        } else {
          j._output = [];
          jobs.value.unshift(j);
        }
      }
    }

    async function startJob(type, params = {}) {
      const res = await api('/api/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type, site: site.value, component: component.value, params })
      });
      res._output = [];
      jobs.value.unshift(res);
      activeJobs[type] = res.id;
      connectSSE(res.id);
      _schedulePoll();
      return res;
    }

    async function cancelJob(id) {
      await api(`/api/jobs/${id}`, { method: 'DELETE' });
      refreshJobs();
    }

    const discoverJobId = ref(null);
    const discoverRunning = computed(() => {
      if (!discoverJobId.value) return false;
      const j = jobs.value.find(x => x.id === discoverJobId.value);
      return j && j.status === 'running';
    });
    const manualDiscoverJobId = ref(null);
    const manualDiscoverRunning = computed(() => {
      if (!manualDiscoverJobId.value) return false;
      const j = jobs.value.find(x => x.id === manualDiscoverJobId.value);
      return j && j.status === 'running';
    });

    const companyDiscoverJobId = ref(null);
    const companyDiscoverRunning = computed(() => {
      if (!companyDiscoverJobId.value) return false;
      const j = jobs.value.find(x => x.id === companyDiscoverJobId.value);
      return j && j.status === 'running';
    });
    const companyDiscoverDone = computed(() => {
      if (!companyDiscoverJobId.value) return false;
      const j = jobs.value.find(x => x.id === companyDiscoverJobId.value);
      return j && j.status === 'done';
    });
    const sampleRequestRunning = computed(() => {
      const jid = activeJobs.sample_request;
      if (!jid) return false;
      const j = jobs.value.find(x => x.id === jid);
      return j && (j.status === 'running' || j.status === 'pending');
    });

    const loginJobId = ref(null);
    const loginRunning = computed(() => {
      if (!loginJobId.value) return false;
      const j = jobs.value.find(x => x.id === loginJobId.value);
      return j && j.status === 'running';
    });
    const loginUrl = ref('');
    const authConfigured = ref(false);

    async function loadAuthStatus() {
      if (!site.value) { authConfigured.value = false; loginUrl.value = ''; return; }
      const _isLocal = site.value.startsWith('localhost') || site.value.startsWith('127.') || site.value.startsWith('0.0.0.0');
      loginUrl.value = `${_isLocal ? 'http' : 'https'}://${site.value}`;
      try {
        const s = await api(`/api/sites/${encodeURIComponent(site.value)}/auth-status`);
        authConfigured.value = s.configured;
      } catch { authConfigured.value = false; }
    }

    async function checkSetupAndNavigate() {
      if (!site.value || !component.value) return;
      await loadAuthStatus();
      try {
        const data = await api(`/api/sites/${encodeURIComponent(site.value)}/${encodeURIComponent(component.value)}/config`);
        if (!authConfigured.value || !data.submission) {
          tab.value = 'discover';
        }
      } catch { /* ignore */ }
    }

    async function onComponentChange() {
      await loadContext();
      await checkSetupAndNavigate();
    }

    async function startLogin() {
      const j = await startJob('login', { url: loginUrl.value });
      loginJobId.value = j.id;
    }

    async function sendLoginEnter() {
      if (loginJobId.value) {
        await api(`/api/jobs/${loginJobId.value}/stdin`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: '\n' })
        });
        await new Promise(r => setTimeout(r, 1200));
        await loadAuthStatus();
      }
    }

    async function startGenerate() {
      await startJob('generate', { strategy: gen.strategy, framework: gen.framework });
    }

    async function startCompanyDiscover() {
      const j = await startJob('company_discovery');
      companyDiscoverJobId.value = j.id;
    }

    async function sendCompanyDiscoverEnter() {
      if (companyDiscoverJobId.value) {
        await api(`/api/jobs/${companyDiscoverJobId.value}/stdin`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: '\n' })
        });
      }
    }

    async function startDiscover() {
      const j = await startJob('discover');
      discoverJobId.value = j.id;
    }

    async function startManualDiscover() {
      const j = await startJob('manual_discover');
      manualDiscoverJobId.value = j.id;
    }

    async function sendEnter() {
      if (discoverJobId.value) {
        await api(`/api/jobs/${discoverJobId.value}/stdin`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: '\n' })
        });
      }
    }

    async function startRunTests() {
      runProgress.value = null;
      if (run.strategy === '__all__') {
        await startJob('run_tests', { suite: '__all__', framework: run.framework, assess: run.assess });
      } else {
        await startJob('run_tests', { suite: run.framework, assess: run.assess });
      }
    }

    async function startSampleRequest() {
      await startJob('sample_request', { prompt: 'capital of england' });
    }

    async function startRiskAssess() {
      runProgress.value = null;
      await startJob('risk_assess', { compliance_log: risk.log });
    }

    async function startExport() {
      expResult.value = null;
      const job = await startJob('export', { report: exp.report, program_id: exp.program_id });
      if (job && job.id) {
        const poll = setInterval(async () => {
          const j = await api(`/api/jobs/${job.id}`);
          if (j.status !== 'running' && j.status !== 'pending') {
            clearInterval(poll);
            try { expResult.value = await api(`/api/jobs/${job.id}/export-result`); } catch { /* ignore */ }
          }
        }, 2000);
      }
    }

    async function startClearCache() {
      await startJob('clear_cache', { delete_on_server: cache.deleteOnServer });
    }

    let _pollTimer = null;

    function _schedulePoll() {
      if (_pollTimer) return;
      _pollTimer = setInterval(async () => {
        const hasRunning = jobs.value.some(j => j.status === 'running' || j.status === 'pending');
        if (hasRunning) {
          await refreshJobs();
        } else {
          clearInterval(_pollTimer);
          _pollTimer = null;
        }
      }, 10000);
    }

    async function openModal() {
      modalError.value = '';
      modalMsg.value = '';
      modalNewSite.value = '';
      modalNewComponent.value = '';
      if (site.value) {
        modalSite.value = site.value;
        modalComponents.value = components.value.length ? [...components.value] : await api(`/api/sites/${encodeURIComponent(site.value)}/components`);
        modalComponent.value = component.value || '';
        modalRenameSite.value = modalSite.value;
        modalRenameComponent.value = modalComponent.value;
      } else {
        // Pre-fill from .env defaults if available
        try {
          const defaults = await api('/api/env-defaults');
          if (defaults.target) {
            modalSite.value = defaults.target;
            modalComponents.value = await api(`/api/sites/${encodeURIComponent(defaults.target)}/components`);
            if (defaults.component) modalComponent.value = defaults.component;
            else modalComponent.value = '';
            modalRenameSite.value = modalSite.value;
            modalRenameComponent.value = modalComponent.value;
          } else {
            modalSite.value = '';
            modalComponent.value = '';
            modalRenameSite.value = '';
            modalRenameComponent.value = '';
            modalComponents.value = [];
          }
        } catch {
          modalSite.value = '';
          modalComponent.value = '';
          modalRenameSite.value = '';
          modalRenameComponent.value = '';
          modalComponents.value = [];
        }
      }
      showModal.value = true;
    }

    onMounted(async () => {
      await loadSites();
      await refreshJobs();
      if (!site.value) {
        // Check .env for TARGET / COMPONENT defaults — skip modal if both are set
        try {
          const defaults = await api('/api/env-defaults');
          if (defaults.target && defaults.component) {
            const s = defaults.target;
            const comps = await api(`/api/sites/${encodeURIComponent(s)}/components`);
            if (comps.includes(defaults.component)) {
              site.value = s;
              components.value = comps;
              component.value = defaults.component;
              await loadContext();
              await checkSetupAndNavigate();
              return; // skip modal entirely
            }
          }
        } catch { /* fall through to modal */ }
        openModal();
      }
    });

    watch(tab, () => {
      if (tab.value === 'settings') {
        if (settingsTab.value === 'browser') loadConfig();
        else if (settingsTab.value === 'component') loadCompCfg();
        else if (settingsTab.value === 'rubrics') loadRubrics();
      } else if (tab.value === 'export') loadExpCreds();
      else if (tab.value === 'tests') tmLoadStrategies();
      else if (tab.value === 'discover') loadAuthStatus();
      else if (site.value && component.value) loadContext();
    });

    watch(settingsTab, () => {
      if (tab.value !== 'settings') return;
      if (settingsTab.value === 'browser') loadConfig();
      else if (settingsTab.value === 'component') loadCompCfg();
      else if (settingsTab.value === 'rubrics') loadRubrics();
    });

    return {
      site, component, sites, components, tab, settingsTab, tabs, jobsOpen, jobs, activeJobs,
      showRunTroubleshoot,
      allStrategies, allFrameworks, runStrategies, runFrameworks, runAllFrameworks, logs,
      gen, run, risk, exp, cache,
      showModal, modalSite, modalComponent, modalComponents, modalNewSite, modalNewComponent,
      modalRenameSite, modalRenameComponent, modalError, modalMsg,
      onModalSiteChange, onModalComponentChange, confirmModal, openModal,
      modalCreateSite, modalRenameSiteAction, modalDeleteSite,
      modalCreateComponent, modalRenameComponentAction, modalDeleteComponent,
      HINTS, hintDismissed, dismissHint,
      runResults, runResultsLoading, expandedRunRows, toggleRunRow,
      compCfg, compCfgSaved, compCfgError, compCfgEmpty, INPUT_TYPES,
      loadCompCfg, saveCompCfg, addInput, removeInput,
      companyRubricText, companySaved, companyError, companyGenerating, companyGenerateUrl,
      saveCompanyRubric, generateCompanyRubric,
      componentRubricText, componentRubricSaved, componentRubricError,
      componentRubricGenerating, componentGenerateUrl,
      saveComponentRubric, generateComponentRubric,
      cfg, cfgSaved, cfgError,
      BLOCKED_OPTIONS, COUNTRIES, CHANNELS, FETCH_METHODS,
      discoverJobId, discoverRunning, manualDiscoverJobId, manualDiscoverRunning,
      companyDiscoverJobId, companyDiscoverRunning, companyDiscoverDone,
      sampleRequestRunning,
      startCompanyDiscover, sendCompanyDiscoverEnter,
      loginJobId, loginRunning, loginUrl, authConfigured,
      startLogin, sendLoginEnter,
      pretty, lineClass, activeOutput, runProgress, runProgressBarLabel, runProgressEtaText, riskTabProgressBarVisible, formatRunEta,
      onSiteChange, onComponentChange, loadContext, loadRunFrameworks, refreshRunTests,
      tmStrategy, tmStrategies, tmFramework, tmFrameworks, tmFile, tmDirty, tmSaving, tmSaveMsg,
      tmEditingId, tmAddingMandate, tmNewPrompt, tmImportFile, tmImportName, tmImporting, tmImportMsg,
      tmLoadStrategies, tmLoadFrameworks, tmLoadFile, tmSave, tmDeletePrompt, tmStartAdd, tmConfirmAdd, tmMarkDirty,
      tmImportFileChanged, tmImportZeroShot,
      startGenerate, startDiscover, startManualDiscover, sendEnter,
      startRunTests, startSampleRequest, startRiskAssess, startExport, startClearCache,
      expResult, expPreview, expCreds, expCredsEdit, expCredsSaving, expCredsMsg,
      loadExpCreds, saveExpCreds, clearExpCreds,
      cancelJob, saveConfig, toggleBlocked,
    };
  }
}).mount('#app');

