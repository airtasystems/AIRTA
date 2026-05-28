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
    const showRunLoginModal = ref(false);
    const showRunRateLimitModal = ref(false);
    const runLoginUrl = ref('');

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
    const runTestFiles = ref([]);
    const logs = reactive({ runs: [], compliance: [], reports: [] });

    // --- Test Management tab ---
    const tmStrategy = ref('');
    const tmFramework = ref('');
    const tmStrategies = ref([]);
    const tmFrameworks = ref([]);
    const tmTestFiles = ref([]);
    const tmFile = ref(null);       // loaded test file { framework, description, mandates }
    const tmDirty = ref(false);
    const tmSaving = ref(false);
    const tmSaveMsg = ref('');
    const tmEditingId = ref(null);  // prompt id being inline-edited
    const tmAddingMandate = ref('');// mandate slug for new-prompt form
    const tmNewPrompt = reactive({ id: '', description: '', prompt: '', prompts: [] });
    const tmImportFile = ref(null);
    const tmImportName = ref('');
    const tmImporting = ref(false);
    const tmImportMsg = ref('');
    const showTmImportHelpModal = ref(false);

    const TM_MULTI_TURN_STRATEGIES = {
      'multi-shot': 3,
      'tree-of-thoughts': 4,
      iterative: 4,
      'prompt-chaining': 3,
    };

    function tmIsMultiTurnStrategy() {
      return Object.prototype.hasOwnProperty.call(TM_MULTI_TURN_STRATEGIES, tmStrategy.value);
    }

    function tmDefaultTurnCount() {
      return TM_MULTI_TURN_STRATEGIES[tmStrategy.value] || 3;
    }

    function tmEntryIsMultiTurn(entry) {
      return Array.isArray(entry?.prompts) && entry.prompts.length > 0;
    }

    function tmPromptPreview(entry) {
      if (tmEntryIsMultiTurn(entry)) {
        const first = (entry.prompts[0] || '').trim();
        const extra = entry.prompts.length - 1;
        if (extra > 0) {
          return `${first}\n\n(${extra} more turn${extra === 1 ? '' : 's'})`;
        }
        return first;
      }
      return entry?.prompt || '';
    }

    function tmTurnLabel(index, total) {
      if (tmStrategy.value === 'tree-of-thoughts') {
        return ['Setup', 'Propose', 'Evaluate', 'Select'][index] || `Turn ${index + 1}`;
      }
      if (tmStrategy.value === 'prompt-chaining') {
        return `Step ${index + 1}`;
      }
      return `Turn ${index + 1}${total > 1 ? ` / ${total}` : ''}`;
    }

    function tmEnsurePromptTurns(entry) {
      if (!Array.isArray(entry.prompts)) {
        entry.prompts = Array.from({ length: tmDefaultTurnCount() }, () => '');
      }
      return entry.prompts;
    }

    function tmAddTurn(entry) {
      tmEnsurePromptTurns(entry).push('');
      tmMarkDirty();
    }

    function tmRemoveTurn(entry, turnIdx) {
      if (!Array.isArray(entry.prompts) || entry.prompts.length <= 1) return;
      entry.prompts.splice(turnIdx, 1);
      tmMarkDirty();
    }

    function tmResetNewPrompt() {
      tmNewPrompt.id = '';
      tmNewPrompt.description = '';
      tmNewPrompt.prompt = '';
      tmNewPrompt.prompts = Array.from({ length: tmDefaultTurnCount() }, () => '');
    }

    const tmVisibleFrameworks = computed(() => {
      const seen = new Map();
      for (const f of tmTestFiles.value) {
        if (!seen.has(f.slug)) seen.set(f.slug, { slug: f.slug, label: f.label });
      }
      return Array.from(seen.values());
    });

    const tmVisibleStrategies = computed(() => {
      if (!tmFramework.value) return [];
      const available = new Set(
        tmTestFiles.value
          .filter(f => f.slug === tmFramework.value)
          .map(f => f.strategy),
      );
      return tmStrategies.value.filter(s => available.has(s.slug));
    });

    function tmSelectedTestFile() {
      if (!tmFramework.value || !tmStrategy.value) return null;
      return tmTestFiles.value.find(
        f => f.slug === tmFramework.value && f.strategy === tmStrategy.value,
      ) || null;
    }

    async function tmLoadStrategies() {
      tmStrategies.value = [];
      tmStrategy.value = '';
      tmFrameworks.value = [];
      tmTestFiles.value = [];
      tmFramework.value = '';
      tmFile.value = null;
      if (!site.value || !component.value) return;
      const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
      const strategies = await api(`/api/sites/${s}/${c}/strategies`);
      tmStrategies.value = strategies.slice().reverse();

      const allFiles = [];
      for (const strat of tmStrategies.value) {
        const files = await api(`/api/sites/${s}/${c}/strategies/${encodeURIComponent(strat.slug)}/frameworks`);
        for (const f of files.slice().reverse()) {
          allFiles.push({ ...f, strategy: strat.slug });
        }
      }
      tmTestFiles.value = allFiles;
    }

    async function tmOnStrategyChange() {
      tmFile.value = null;
      tmDirty.value = false;
      if (tmFramework.value && tmStrategy.value) {
        await tmLoadFile();
      }
    }

    async function tmOnFrameworkChange() {
      tmFile.value = null;
      tmDirty.value = false;
      tmEditingId.value = null;
      tmAddingMandate.value = '';
      if (!tmFramework.value) {
        tmStrategy.value = '';
        return;
      }
      if (tmStrategy.value && !tmVisibleStrategies.value.some(s => s.slug === tmStrategy.value)) {
        tmStrategy.value = '';
      }
      if (tmFramework.value && tmVisibleStrategies.value.length === 1) {
        tmStrategy.value = tmVisibleStrategies.value[0].slug;
      }
      if (tmFramework.value && tmStrategy.value) {
        await tmLoadFile();
      }
    }

    async function tmLoadFrameworks() {
      await tmOnFrameworkChange();
    }

    async function tmLoadFile() {
      tmFile.value = null;
      tmDirty.value = false;
      tmEditingId.value = null;
      tmAddingMandate.value = '';
      if (!tmFramework.value || !tmStrategy.value) return;
      const picked = tmSelectedTestFile();
      if (!picked) return;
      const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
      const strat = encodeURIComponent(tmStrategy.value);
      tmFile.value = await api(`/api/sites/${s}/${c}/tests/${strat}/${encodeURIComponent(picked.slug)}`);
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
        await api(`/api/sites/${s}/${c}/tests/${strat}/${encodeURIComponent(tmFramework.value)}`, {
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
      tmResetNewPrompt();
    }

    function tmConfirmAdd(mandateIdx) {
      const id = tmNewPrompt.id.trim();
      const description = tmNewPrompt.description.trim();
      if (!id) return;

      let entry;
      if (tmIsMultiTurnStrategy()) {
        const prompts = (tmNewPrompt.prompts || []).map(t => String(t || '').trim());
        if (!prompts.some(Boolean)) return;
        entry = { id, description, prompts };
      } else {
        const prompt = tmNewPrompt.prompt.trim();
        if (!prompt) return;
        entry = { id, description, prompt };
      }

      tmFile.value.mandates[mandateIdx].prompts.push(entry);
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
        tmFramework.value = result.framework;
        tmStrategy.value = result.strategy;
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

    const runVisibleFrameworks = computed(() => {
      const seen = new Map();
      for (const f of runTestFiles.value) {
        if (!seen.has(f.slug)) seen.set(f.slug, { slug: f.slug, label: f.label });
      }
      return Array.from(seen.values());
    });

    const runVisibleStrategies = computed(() => {
      if (!run.framework) return [];
      const available = new Set(
        runTestFiles.value
          .filter(f => f.slug === run.framework)
          .map(f => f.strategy),
      );
      return runStrategies.value.filter(s => available.has(s.slug));
    });

    function runSelectedTestFile() {
      if (!run.framework || !run.strategy || run.strategy === '__all__') return null;
      return runTestFiles.value.find(
        f => f.slug === run.framework && f.strategy === run.strategy,
      ) || null;
    }

    async function loadRunTestCatalog({ preserveSelection = false } = {}) {
      const prevFramework = preserveSelection ? run.framework : '';
      const prevStrategy = preserveSelection ? run.strategy : '';
      runStrategies.value = [];
      runTestFiles.value = [];
      if (!preserveSelection) {
        run.framework = '';
        run.strategy = '';
      }
      if (!site.value || !component.value) return;
      const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
      const strategies = await api(`/api/sites/${s}/${c}/strategies`);
      runStrategies.value = strategies.slice().reverse();
      const allFiles = [];
      for (const strat of runStrategies.value) {
        const files = await api(`/api/sites/${s}/${c}/strategies/${encodeURIComponent(strat.slug)}/frameworks`);
        for (const f of files.slice().reverse()) {
          allFiles.push({ ...f, strategy: strat.slug });
        }
      }
      runTestFiles.value = allFiles;
      if (preserveSelection && prevFramework && runVisibleFrameworks.value.some(f => f.slug === prevFramework)) {
        run.framework = prevFramework;
        if (prevStrategy === '__all__') {
          run.strategy = '__all__';
        } else if (prevStrategy && runVisibleStrategies.value.some(st => st.slug === prevStrategy)) {
          run.strategy = prevStrategy;
        } else {
          run.strategy = '';
        }
      }
    }

    function onRunFrameworkChange() {
      if (!run.framework) {
        run.strategy = '';
        return;
      }
      if (run.strategy && run.strategy !== '__all__' && !runVisibleStrategies.value.some(s => s.slug === run.strategy)) {
        run.strategy = '';
      }
      if (runVisibleStrategies.value.length === 1 && run.strategy !== '__all__') {
        run.strategy = runVisibleStrategies.value[0].slug;
      }
    }

    function onRunStrategyChange() {
      // Selection validated via computed lists and startRunTests guard.
    }
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
          count: (data.compliance_results || []).length,
          framework: data.framework || '',
          timestamp: data.timestamp || '',
        };
      } catch { /* ignore */ }
    });
    const cache = reactive({ deleteOnServer: false, useGeminiCache: false, effectiveGeminiCache: false, componentOverride: null });
    const cacheSettingsSaving = ref(false);
    const cacheSettingsMsg = ref('');

    async function loadCacheSettings() {
      try {
        let path = '/api/cache-settings';
        if (site.value && component.value) {
          const s = encodeURIComponent(site.value);
          const c = encodeURIComponent(component.value);
          path += `?site=${s}&component=${c}`;
        }
        const s = await api(path);
        cache.useGeminiCache = !!s.gemini_use_cache;
        cache.effectiveGeminiCache = !!s.effective_gemini_use_cache;
        cache.componentOverride = s.component_override;
      } catch { /* ignore */ }
    }

    async function saveCacheSettings() {
      cacheSettingsSaving.value = true;
      cacheSettingsMsg.value = '';
      try {
        const result = await api('/api/cache-settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ gemini_use_cache: cache.useGeminiCache }),
        });
        cache.useGeminiCache = !!result.gemini_use_cache;
        cacheSettingsMsg.value = cache.useGeminiCache ? 'Gemini cache enabled' : 'Gemini cache disabled';
      } catch (e) {
        cacheSettingsMsg.value = 'Save failed: ' + e.message;
      } finally {
        cacheSettingsSaving.value = false;
      }
    }

    // Component config
    const PROMPT_TEMPLATE_HINT = '{{prompt}}';
    const PROMPT_MODEL_HINT = '{{model}}';
    const PROMPT_BODY_PLACEHOLDER = '{"prompt": "' + PROMPT_TEMPLATE_HINT + '"}';

    const INPUT_TYPES = ['text', 'textarea', 'contenteditable', 'password', 'email', 'search', 'select', 'combobox', 'checkbox', 'radio'];
    const compCfg = reactive({
      login_url: '',
      submission: {
        transport: 'ui',
        start_url: '', inputs: [], submit_selector: '', response_selector: '',
        response_within_selector: '', response_text_within_selector: '',
        submit_via: 'click', response_wait_ms: 5000,
        api_url: '', api_method: 'POST', api_response_path: 'response', api_model: '',
        api_body_json: '{\n  "prompt": "{{prompt}}"\n}',
        api_headers_json: '{}',
      },
    });
    const llmApiPresets = ref([]);
    const settingsSchema = ref(null);
    const compSettings = reactive({});
    const compSettingsInherited = reactive({});
    const submissionTransport = ref('ui');
    const runShowsBrowserPreview = computed(
      () => (submissionTransport.value || 'ui').toLowerCase() !== 'api',
    );
    const compCfgSaved = ref(false);
    const compCfgError = ref('');
    const compCfgEmpty = ref(false);

    function settingMeta(key) {
      return (settingsSchema.value?.meta || {})[key] || { type: 'string', label: key };
    }

    function settingLabel(key) {
      return settingMeta(key).label || key;
    }

    function formatSettingGlobal(key) {
      const val = settingsSchema.value?.globals?.[key];
      if (key === 'BLOCKED_TYPES') {
        const arr = Array.isArray(val) ? val : [];
        return arr.length ? arr.join(', ') : '(none)';
      }
      if (typeof val === 'boolean') return val ? 'on' : 'off';
      if (val === null || val === undefined || val === '') return '(empty)';
      return String(val);
    }

    function cloneSettingGlobal(key) {
      const val = settingsSchema.value?.globals?.[key];
      if (key === 'BLOCKED_TYPES') return Array.isArray(val) ? [...val] : [];
      if (typeof val === 'boolean') return val;
      if (val === null || val === undefined) return '';
      return val;
    }

    function initCompSettingsFromConfig(overrides) {
      if (!settingsSchema.value) return;
      for (const group of settingsSchema.value.groups || []) {
        for (const key of group.keys || []) {
          const inherited = !(key in (overrides || {}));
          compSettingsInherited[key] = inherited;
          if (inherited) {
            compSettings[key] = cloneSettingGlobal(key);
          } else {
            const raw = overrides[key];
            if (key === 'BLOCKED_TYPES') {
              compSettings[key] = Array.isArray(raw) ? [...raw] : [];
            } else if (typeof settingsSchema.value.globals?.[key] === 'boolean') {
              compSettings[key] = !!raw;
            } else {
              compSettings[key] = raw;
            }
          }
        }
      }
    }

    function onCompSettingInheritChange(key) {
      if (compSettingsInherited[key]) {
        compSettings[key] = cloneSettingGlobal(key);
      }
    }

    function toggleCompSettingSet(key, type) {
      if (!Array.isArray(compSettings[key])) compSettings[key] = [];
      const idx = compSettings[key].indexOf(type);
      if (idx === -1) compSettings[key].push(type);
      else compSettings[key].splice(idx, 1);
    }

    function buildCompSettingsPayload() {
      const settings = {};
      if (!settingsSchema.value) return settings;
      for (const group of settingsSchema.value.groups || []) {
        for (const key of group.keys || []) {
          if (compSettingsInherited[key]) continue;
          const val = compSettings[key];
          if (key === 'BLOCKED_TYPES') {
            settings[key] = Array.isArray(val) ? [...val].sort() : [];
          } else {
            settings[key] = val;
          }
        }
      }
      return settings;
    }

    async function loadSettingsSchema() {
      try {
        settingsSchema.value = await api('/api/settings-schema');
      } catch { /* ignore */ }
    }

    function submissionConfigComplete(sub) {
      if (!sub || typeof sub !== 'object') return false;
      const transport = (sub.transport || 'ui').toLowerCase();
      if (transport === 'api') {
        return !!(sub.api_url || sub.start_url);
      }
      return !!(sub.start_url && sub.submit_selector && (sub.inputs?.length || sub.input_selector));
    }

    function applySubmissionToCompCfg(sub) {
      const s = sub || {};
      compCfg.submission.transport = s.transport === 'api' ? 'api' : 'ui';
      submissionTransport.value = compCfg.submission.transport;
      compCfg.submission.start_url = s.start_url || '';
      compCfg.submission.submit_selector = s.submit_selector || '';
      compCfg.submission.response_selector = s.response_selector || '';
      compCfg.submission.response_within_selector = s.response_within_selector || '';
      compCfg.submission.response_text_within_selector = s.response_text_within_selector || '';
      compCfg.submission.submit_via = s.submit_via || 'click';
      compCfg.submission.response_wait_ms = s.response_wait_ms ?? 5000;
      compCfg.submission.inputs = (s.inputs || []).map(inp => ({ ...inp }));
      compCfg.submission.api_url = s.api_url || '';
      compCfg.submission.api_method = s.api_method || 'POST';
      compCfg.submission.api_response_path = s.api_response_path || 'response';
      compCfg.submission.api_model = s.api_model || '';
      compCfg.submission.api_body_json = JSON.stringify(s.api_body || { prompt: '{{prompt}}' }, null, 2);
      compCfg.submission.api_headers_json = JSON.stringify(s.api_headers || {}, null, 2);
      if (compCfg.submission.transport === 'api') {
        discoverTransport.value = 'api';
        syncApiDiscoverFromCompCfg();
      }
    }

    function syncApiDiscoverFromCompCfg() {
      apiDiscover.url = compCfg.submission.api_url;
      apiDiscover.method = compCfg.submission.api_method;
      apiDiscover.responsePath = compCfg.submission.api_response_path;
      apiDiscover.model = compCfg.submission.api_model;
      apiDiscover.bodyJson = compCfg.submission.api_body_json;
      apiDiscover.headersJson = compCfg.submission.api_headers_json;
    }

    function buildSubmissionPayload() {
      const transport = compCfg.submission.transport || 'ui';
      if (transport === 'api') {
        let api_body = { prompt: '{{prompt}}' };
        let api_headers = {};
        try { api_body = JSON.parse(compCfg.submission.api_body_json || '{}'); } catch { /* keep default */ }
        try { api_headers = JSON.parse(compCfg.submission.api_headers_json || '{}'); } catch { /* ignore */ }
        const out = {
          transport: 'api',
          api_url: compCfg.submission.api_url,
          api_method: compCfg.submission.api_method || 'POST',
          api_headers,
          api_body,
          api_response_path: compCfg.submission.api_response_path || 'response',
        };
        if ((compCfg.submission.api_model || '').trim()) {
          out.api_model = compCfg.submission.api_model.trim();
        }
        return out;
      }
      return {
        transport: 'ui',
        start_url: compCfg.submission.start_url,
        inputs: compCfg.submission.inputs.map(i => ({ ...i })),
        submit_selector: compCfg.submission.submit_selector,
        response_selector: compCfg.submission.response_selector,
        response_within_selector: compCfg.submission.response_within_selector || '',
        response_text_within_selector: compCfg.submission.response_text_within_selector || '',
        submit_via: compCfg.submission.submit_via,
        response_wait_ms: Number(compCfg.submission.response_wait_ms),
      };
    }

    async function loadCompCfg() {
      if (!site.value || !component.value) return;
      compCfgError.value = '';
      try {
        await loadSettingsSchema();
        const data = await api(`/api/sites/${encodeURIComponent(site.value)}/${encodeURIComponent(component.value)}/config`);
        compCfg.login_url = data.login_url || '';
        applySubmissionToCompCfg(data.submission);
        compCfgEmpty.value = !submissionConfigComplete(data.submission);
        initCompSettingsFromConfig(data.settings || {});
      } catch (e) { compCfgError.value = String(e); }
    }

    async function saveCompCfg() {
      compCfgError.value = '';
      compCfgSaved.value = false;
      try {
        const existing = await api(`/api/sites/${encodeURIComponent(site.value)}/${encodeURIComponent(component.value)}/config`);
        const payload = {
          ...existing,
          login_url: compCfg.login_url,
          submission: buildSubmissionPayload(),
        };
        const settings = buildCompSettingsPayload();
        if (Object.keys(settings).length) payload.settings = settings;
        else delete payload.settings;
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
        try {
          const cfg = await api(`/api/sites/${encodeURIComponent(site.value)}/${encodeURIComponent(component.value)}/config`);
          if (cfg?.submission?.start_url) {
            componentGenerateUrl.value = cfg.submission.start_url;
          } else if (!componentGenerateUrl.value) {
            componentGenerateUrl.value = `https://${site.value}`;
          }
        } catch {
          if (!componentGenerateUrl.value) {
            componentGenerateUrl.value = `https://${site.value}`;
          }
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
        title: 'Connect Target',
        text: 'UI components: Step 1 log in to the target app, Step 3 record browser selectors. LLM APIs: Step 1 save an API key (or public access for open endpoints), Step 3 pick an API format preset, set URL/model/body/response path, then Connect via API. Run Tests sends prompts through whichever transport is in config.yaml.',
      },
      run: {
        title: 'Run Tests',
        text: 'Submits each test prompt to the target using browser UI or API transport (from Connect Target). Select a strategy and framework, then Run. Use Send Sample Request to verify the connection. Results appear in the table and are saved to a timestamped log directory.',
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
        text: 'Sends a pipeline report to an AIRTA Systems instance via the bulk-import API. Select a report, enter your host, API key, and program ID. Each compliance test result is submitted as a finding.',
      },
      cache: {
        title: 'Clear Cache',
        text: 'Global default for Gemini context cache (stored in .env). Per-component overrides in config.yaml → settings take precedence for cache and browser config. Clear All Caches removes Gemini handles, on-disk risk assessment results ([cache hit]), and __pycache__ folders.',
      },
      component: {
        title: 'Component Config',
        text: 'Configures how browser-bot interacts with this component\'s UI - the page URL, input selector, submit button, and where to read the AI response from. Settings overrides mirror Browser Config and Cache Control; omitted keys inherit via site config, global config.py/.env, then config.defaults.yaml.',
      },
      config: {
        title: 'Global Config',
        text: 'Controls browser-bot\'s global behaviour. Changes are written directly to config.py and override config.defaults.yaml. Per-site or per-component overrides in config.yaml → settings take precedence on the next run.',
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

    async function loadLogs() {
      if (!site.value || !component.value) return;
      const s = encodeURIComponent(site.value), c = encodeURIComponent(component.value);
      const l = await api(`/api/sites/${s}/${c}/logs`);
      logs.runs = l.runs;
      logs.compliance = l.compliance;
      logs.reports = l.reports;
    }

    async function loadLatestRunLog() {
      if (!site.value || !component.value) return;
      runResultsLoading.value = true;
      try {
        await loadLogs();
        if (!logs.runs.length) return;
        const data = await api(`/api/files?path=${encodeURIComponent(logs.runs[0].path)}`);
        expandedRunRows.value = {};
        if (data.mode === 'multi') {
          runResults.value = (data.batches || []).map(b => {
            const turns = b.turns || [];
            const last = turns[turns.length - 1] || {};
            return {
              label: `#${b.batch_index + 1}${turns.length > 1 ? ` (${turns.length}-turn, final)` : ''}`,
              input: last.input || '',
              response: last.response,
              turns,
            };
          });
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
    const runPreviewSlots = ref([{ slot: 0, url: '' }]);
    const runPreviewLightbox = ref(null);
    const runBlockedInfo = ref(null);
    const runRateLimitBackoff = ref(120);
    const rateLimitCountdown = ref(0);
    const rateLimitWaiting = ref(false);
    let rateLimitCountdownTimer = null;

    function initRunPreviewSlots(count) {
      const n = Math.max(1, Number(count) || 1);
      runPreviewSlots.value = Array.from({ length: n }, (_, slot) => ({ slot, url: '' }));
    }

    function setRunPreviewSlot(jobId, slot) {
      const idx = Number(slot) || 0;
      const url = `${API}/api/jobs/${jobId}/preview?slot=${idx}&t=${Date.now()}`;
      const slots = runPreviewSlots.value.slice();
      while (slots.length <= idx) {
        slots.push({ slot: slots.length, url: '' });
      }
      slots[idx] = { slot: idx, url };
      runPreviewSlots.value = slots;
    }

    function clearRunPreview() {
      runPreviewSlots.value = [{ slot: 0, url: '' }];
      runPreviewLightbox.value = null;
    }

    function openRunPreviewLightbox(slotEntry) {
      if (!slotEntry?.url) return;
      const label = runPreviewSlots.value.length > 1
        ? `Browser ${slotEntry.slot + 1}`
        : 'Live browser preview';
      runPreviewLightbox.value = { url: slotEntry.url, label };
    }

    function closeRunPreviewLightbox() {
      runPreviewLightbox.value = null;
    }

    function formatRunEta(sec) {
      if (sec == null || sec === '' || Number.isNaN(Number(sec))) return '-';
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
      if (p.type === 'blocked') return p.message || 'Run blocked';
      if (p.type === 'rate_limit_wait') return p.message || 'Rate limited - waiting…';
      return `${p.mode === 'multi' ? 'Multi-turn' : 'Single'} · ${p.current ?? 0} / ${p.total ?? 0} prompts`;
    });

    const runProgressEtaText = computed(() => {
      const p = runProgress.value;
      if (!p) return '';
      if (p.type === 'risk_start') return 'Estimating…';
      if (p.phase === 'risk' || p.type === 'risk_progress' || p.type === 'risk_done') {
        if (p.type === 'risk_done') return `${formatRunEta(p.elapsed_sec)} total`;
        if (p.eta_sec != null && p.eta_sec !== '') return `ETA ${formatRunEta(p.eta_sec)} · ${formatRunEta(p.elapsed_sec)} elapsed`;
        return '-';
      }
      if (p.type === 'run_start' || p.type === 'suite') return 'Estimating…';
      if (p.type === 'run_done') return `${formatRunEta(p.elapsed_sec)} total`;
      if (p.eta_sec != null && p.eta_sec !== '') return `ETA ${formatRunEta(p.eta_sec)} · ${formatRunEta(p.elapsed_sec)} elapsed`;
      return '-';
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
        if (components.value.length === 1) {
          component.value = components.value[0];
          await onComponentChange();
        } else {
          await loadAuthStatus();
        }
      } else {
        components.value = [];
      }
    }

    async function loadSubmissionTransport() {
      if (!site.value || !component.value) {
        submissionTransport.value = 'ui';
        return;
      }
      try {
        const data = await api(
          `/api/sites/${encodeURIComponent(site.value)}/${encodeURIComponent(component.value)}/config`,
        );
        const t = (data.submission?.transport || 'ui').toLowerCase();
        submissionTransport.value = t === 'api' ? 'api' : 'ui';
      } catch {
        submissionTransport.value = 'ui';
      }
    }

    async function loadContext() {
      if (site.value && component.value) {
        await loadSubmissionTransport();
        await loadRunTestCatalog();
        await loadLogs();
        if (tab.value === 'settings' && settingsTab.value === 'component') loadCompCfg();
        if (tab.value === 'settings' && settingsTab.value === 'rubrics') loadRubrics();
        if (tab.value === 'tests') await tmLoadStrategies();
      }
    }

    async function refreshRunTests() {
      if (!site.value || !component.value) return;
      await loadRunTestCatalog({ preserveSelection: true });
    }

    // Lines to suppress in the run_tests console - individual prompt/response
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
              if (p.type === 'screenshot') {
                if (isRunJob) setRunPreviewSlot(jobId, p.slot ?? 0);
              } else if (p.type === 'preview_layout') {
                if (isRunJob) initRunPreviewSlots(p.slots ?? 1);
              } else if (p.type === 'rate_limit_wait') {
                if (isRunJob) {
                  runProgress.value = {
                    ...p,
                    pct: 0,
                    phase: 'rate_limit_wait',
                  };
                }
              } else {
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
                } else if (p.type === 'blocked') {
                  pct = 0;
                  phase = 'blocked';
                  if (isRunJob) {
                    runBlockedInfo.value = p;
                    if (p.action === 'start_login' || p.action === 'prompt_login' || p.kind === 'login_required') {
                      pendingRunAfterLogin.value = true;
                      runLoginUrl.value = p.login_url || loginUrl.value || '';
                      tab.value = 'run';
                      showRunLoginModal.value = true;
                    } else if (p.action === 'prompt_rate_limit' || p.kind === 'rate_limited') {
                      pendingRunAfterRateLimit.value = true;
                      runRateLimitBackoff.value = Number(p.backoff_sec) || 120;
                      tab.value = 'run';
                      showRunRateLimitModal.value = true;
                    }
                  }
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
          loadLogs();
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
      const j = jobs.value.find(x => x.id === id);
      if (!j || j.status !== 'running') return;
      j.status = 'cancelled';
      try {
        await api(`/api/jobs/${id}`, { method: 'DELETE' });
      } catch {
        j.status = 'running';
        return;
      }
      if (activeJobs[j.type] === id) {
        activeJobs[j.type] = null;
        if (j.type === 'run_tests' || j.type === 'risk_assess') {
          runProgress.value = null;
        }
      }
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
    const pendingRunAfterLogin = ref(false);
    const pendingRunAfterRateLimit = ref(false);
    const loginRunning = computed(() => {
      if (!loginJobId.value) return false;
      const j = jobs.value.find(x => x.id === loginJobId.value);
      return j && j.status === 'running';
    });
    const loginUrl = ref('');
    const authSaving = ref(false);
    const authSaveError = ref('');
    const authConfigured = ref(false);
    const authMode = ref(null);
    const authLoginChoice = ref(null);
    const authPublicSaving = ref(false);
    const authApiKey = ref('');
    const authApiKeyHeader = ref('Authorization');
    const authApiKeyQueryParam = ref('');
    const authUseBearer = ref(true);
    const authApiKeySaving = ref(false);

    async function loadAuthStatus() {
      if (!site.value) {
        authConfigured.value = false;
        authMode.value = null;
        authLoginChoice.value = null;
        loginUrl.value = '';
        return;
      }
      const _isLocal = site.value.startsWith('localhost') || site.value.startsWith('127.') || site.value.startsWith('0.0.0.0');
      loginUrl.value = `${_isLocal ? 'http' : 'https'}://${site.value}`;
      try {
        const s = await api(`/api/sites/${encodeURIComponent(site.value)}/auth-status`);
        authConfigured.value = s.configured;
        authMode.value = s.mode || null;
        if (s.auth_header) authApiKeyHeader.value = s.auth_header;
        if (s.auth_query_param) authApiKeyQueryParam.value = s.auth_query_param;
        if (s.configured) {
          authLoginChoice.value = (s.mode === 'none' || s.mode === 'api_key') ? false : true;
          if (s.mode === 'api_key' && s.auth_header) {
            authUseBearer.value = s.auth_header.toLowerCase() === 'authorization';
          }
        } else {
          authLoginChoice.value = null;
        }
      } catch {
        authConfigured.value = false;
        authMode.value = null;
        authLoginChoice.value = null;
      }
    }

    function chooseAuthRequired() {
      authLoginChoice.value = true;
    }

    function chooseAuthApiKey() {
      authLoginChoice.value = 'api_key';
      authApiKey.value = '';
      if (discoverTransport.value === 'api' && apiDiscover.presetId) {
        applyApiPreset(apiDiscover.presetId, { authOnly: true });
      }
    }

    async function saveAuthApiKey() {
      if (!site.value || !authApiKey.value.trim() || authApiKeySaving.value) return;
      authApiKeySaving.value = true;
      try {
        await api(`/api/sites/${encodeURIComponent(site.value)}/auth/api-key`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            api_key: authApiKey.value.trim(),
            header_name: authApiKeyHeader.value || 'Authorization',
            use_bearer: authUseBearer.value,
            query_param_name: authApiKeyQueryParam.value.trim(),
          }),
        });
        authApiKey.value = '';
        await loadAuthStatus();
      } catch (e) {
        alert('Could not save API key: ' + e.message);
      } finally {
        authApiKeySaving.value = false;
      }
    }

    async function chooseAuthNotRequired() {
      if (!site.value || authPublicSaving.value) return;
      authPublicSaving.value = true;
      try {
        await api(`/api/sites/${encodeURIComponent(site.value)}/auth/public`, { method: 'POST' });
        await loadAuthStatus();
      } catch (e) {
        alert('Could not save public auth: ' + e.message);
      } finally {
        authPublicSaving.value = false;
      }
    }

    async function resetAuthSetup() {
      if (!site.value) return;
      try {
        await api(`/api/sites/${encodeURIComponent(site.value)}/auth`, { method: 'DELETE' });
      } catch {
        /* no auth file yet - still show choice */
      }
      authConfigured.value = false;
      authMode.value = null;
      authLoginChoice.value = null;
      authApiKey.value = '';
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

    async function prepareAuthForLoginCapture() {
      if (authMode.value === 'none' || authMode.value === 'api_key') {
        try {
          await api(`/api/sites/${encodeURIComponent(site.value)}/auth`, { method: 'DELETE' });
        } catch { /* ignore */ }
        authConfigured.value = false;
        authMode.value = null;
      }
      authLoginChoice.value = true;
    }

    async function confirmRunLogin() {
      if (!site.value || loginRunning.value) return;
      const url = runLoginUrl.value || runBlockedInfo.value?.login_url || loginUrl.value;
      if (!url) return;
      loginUrl.value = url;
      runLoginUrl.value = url;
      pendingRunAfterLogin.value = true;
      authSaveError.value = '';
      await prepareAuthForLoginCapture();
      await startLogin(url);
    }

    function dismissRunLoginModal() {
      showRunLoginModal.value = false;
    }

    function onRunTroubleshoot() {
      if (runBlockedInfo.value?.kind === 'login_required' || runBlockedInfo.value?.action === 'prompt_login' || runBlockedInfo.value?.action === 'start_login') {
        showRunLoginModal.value = true;
        return;
      }
      if (runBlockedInfo.value?.kind === 'rate_limited' || runBlockedInfo.value?.action === 'prompt_rate_limit') {
        showRunRateLimitModal.value = true;
        return;
      }
      showRunTroubleshoot.value = true;
    }

    function formatRateLimitWait(sec) {
      const n = Math.max(0, Math.round(Number(sec) || 0));
      if (n < 60) return `${n}s`;
      const m = Math.floor(n / 60);
      const s = n % 60;
      return s ? `${m}m ${s}s` : `${m}m`;
    }

    function clearRateLimitCountdown() {
      if (rateLimitCountdownTimer) {
        clearInterval(rateLimitCountdownTimer);
        rateLimitCountdownTimer = null;
      }
      rateLimitCountdown.value = 0;
      rateLimitWaiting.value = false;
    }

    function dismissRunRateLimitModal() {
      clearRateLimitCountdown();
      showRunRateLimitModal.value = false;
    }

    async function retryRunAfterRateLimit(withWait) {
      if (rateLimitWaiting.value) return;
      clearRateLimitCountdown();
      const waitSec = withWait ? Math.max(0, Math.round(Number(runRateLimitBackoff.value) || 120)) : 0;
      if (waitSec > 0) {
        rateLimitWaiting.value = true;
        rateLimitCountdown.value = waitSec;
        await new Promise(resolve => {
          rateLimitCountdownTimer = setInterval(() => {
            rateLimitCountdown.value = Math.max(0, rateLimitCountdown.value - 1);
            if (rateLimitCountdown.value <= 0) {
              clearRateLimitCountdown();
              resolve();
            }
          }, 1000);
        });
      }
      pendingRunAfterRateLimit.value = false;
      runBlockedInfo.value = null;
      showRunRateLimitModal.value = false;
      await startRunTests();
    }

    async function startLogin(urlOverride) {
      const url = (typeof urlOverride === 'string' && urlOverride) || loginUrl.value;
      if (!url) return;
      loginUrl.value = url;
      const j = await startJob('login', { url });
      loginJobId.value = j.id;
    }

    async function saveAuth() {
      if (!loginJobId.value || authSaving.value) return;
      authSaving.value = true;
      authSaveError.value = '';
      try {
        await api(`/api/jobs/${loginJobId.value}/stdin`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: '\n' })
        });
        await new Promise(r => setTimeout(r, 1200));
        await loadAuthStatus();
        if (authMode.value !== 'session') {
          authSaveError.value = 'Auth was not saved. Finish sign-in in the browser, then try again.';
          return;
        }
        authSaveError.value = '';
        if (pendingRunAfterLogin.value) {
          pendingRunAfterLogin.value = false;
          runBlockedInfo.value = null;
          showRunLoginModal.value = false;
          await startRunTests();
        }
      } catch (e) {
        authSaveError.value = 'Could not save auth: ' + e.message;
      } finally {
        authSaving.value = false;
      }
    }

    async function sendLoginEnter() {
      await saveAuth();
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

    const discoverTransport = ref('browser');
    const apiDiscover = reactive({
      presetId: 'custom',
      url: '',
      method: 'POST',
      responsePath: 'response',
      model: '',
      bodyJson: '{\n  "prompt": "{{prompt}}"\n}',
      headersJson: '{}',
    });

    async function loadLlmApiPresets() {
      try {
        const data = await api('/api/llm-api-presets');
        llmApiPresets.value = data.presets || [];
        if (!apiDiscover.url && llmApiPresets.value.length) {
          applyApiPreset(apiDiscover.presetId || 'custom');
        }
      } catch { /* ignore */ }
    }

    function applyApiPreset(presetId, opts = {}) {
      const preset = llmApiPresets.value.find(p => p.id === presetId);
      if (!preset) return;
      apiDiscover.presetId = presetId;
      if (!opts.authOnly) {
        apiDiscover.url = preset.url || '';
        apiDiscover.method = preset.method || 'POST';
        apiDiscover.responsePath = preset.response_path || 'response';
        apiDiscover.model = preset.default_model || '';
        apiDiscover.bodyJson = JSON.stringify(preset.body || { prompt: '{{prompt}}' }, null, 2);
        apiDiscover.headersJson = JSON.stringify(preset.headers || {}, null, 2);
        compCfg.submission.transport = 'api';
        compCfg.submission.api_url = apiDiscover.url;
        compCfg.submission.api_method = apiDiscover.method;
        compCfg.submission.api_response_path = apiDiscover.responsePath;
        compCfg.submission.api_model = apiDiscover.model;
        compCfg.submission.api_body_json = apiDiscover.bodyJson;
        compCfg.submission.api_headers_json = apiDiscover.headersJson;
      }
      if (preset.auth_header) {
        authApiKeyHeader.value = preset.auth_header;
        authUseBearer.value = preset.auth_header.toLowerCase() === 'authorization';
      } else {
        authApiKeyHeader.value = 'Authorization';
        authUseBearer.value = true;
      }
      authApiKeyQueryParam.value = preset.auth_query_param || '';
    }

    function onDiscoverTransportChange() {
      if (discoverTransport.value === 'api') {
        loadLlmApiPresets();
        if (!apiDiscover.url) applyApiPreset(apiDiscover.presetId || 'custom');
      }
    }

    function onSettingsApiPreset(ev) {
      const id = ev?.target?.value;
      if (!id) return;
      applyApiPreset(id);
      syncApiDiscoverFromCompCfg();
      ev.target.value = '';
    }

    const apiNeedsAuth = computed(() => {
      const preset = llmApiPresets.value.find(p => p.id === apiDiscover.presetId);
      return !!(preset && (preset.auth_header || preset.auth_query_param));
    });

    const apiAuthReady = computed(() => {
      if (!apiNeedsAuth.value) return authConfigured.value;
      return authMode.value === 'api_key' && authConfigured.value;
    });
    const apiDiscoverJobId = ref(null);
    const apiDiscoverRunning = computed(() => {
      if (!apiDiscoverJobId.value) return false;
      const j = jobs.value.find(x => x.id === apiDiscoverJobId.value);
      return j && (j.status === 'running' || j.status === 'pending');
    });

    async function startApiDiscover() {
      let api_body = null;
      let api_headers = {};
      try { api_body = JSON.parse(apiDiscover.bodyJson || '{}'); } catch (e) {
        alert('Invalid request body JSON: ' + e.message);
        return;
      }
      try { api_headers = JSON.parse(apiDiscover.headersJson || '{}'); } catch (e) {
        alert('Invalid headers JSON: ' + e.message);
        return;
      }
      if (apiNeedsAuth.value && !apiAuthReady.value) {
        alert('Save an API key in Step 1 (Target access) before connecting.');
        return;
      }
      const model = (apiDiscover.model || '').trim();
      if (apiDiscover.url.includes('{{model}}') && !model) {
        alert('Set the Model field - the API URL contains {{model}} (e.g. gemini-2.0-flash-lite).');
        return;
      }
      const j = await startJob('api_discover', {
        api_url: apiDiscover.url,
        api_method: apiDiscover.method,
        api_response_path: apiDiscover.responsePath,
        api_model: model,
        api_body,
        api_headers,
      });
      apiDiscoverJobId.value = j.id;
    }

    async function startDiscover() {
      const j = await startJob('discover');
      discoverJobId.value = j.id;
    }

    async function startManualDiscover() {
      const j = await startJob('manual_discover');
      manualDiscoverJobId.value = j.id;
    }

    function openComponentSettings() {
      settingsTab.value = 'component';
      tab.value = 'settings';
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
      clearRunPreview();
      runBlockedInfo.value = null;
      showRunLoginModal.value = false;
      showRunRateLimitModal.value = false;
      pendingRunAfterLogin.value = false;
      pendingRunAfterRateLimit.value = false;
      runLoginUrl.value = '';
      authSaveError.value = '';
      clearRateLimitCountdown();
      if (run.strategy === '__all__') {
        await startJob('run_tests', { suite: '__all__', framework: run.framework, assess: run.assess });
      } else {
        const picked = runSelectedTestFile();
        if (!picked) return;
        await startJob('run_tests', { suite: picked.path, assess: run.assess });
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

    watch(discoverTransport, onDiscoverTransportChange);

    onMounted(async () => {
      await loadSites();
      await loadLlmApiPresets();
      await refreshJobs();
      if (!site.value) {
        // Check .env for TARGET / COMPONENT defaults - skip modal if both are set
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
        else if (settingsTab.value === 'cache') loadCacheSettings();
      } else if (tab.value === 'export') {
        loadExpCreds();
        loadLogs();
      } else if (tab.value === 'tests') tmLoadStrategies();
      else if (tab.value === 'discover') loadAuthStatus();
      else if (site.value && component.value) loadContext();
    });

    watch(settingsTab, () => {
      if (tab.value !== 'settings') return;
      if (settingsTab.value === 'browser') loadConfig();
      else if (settingsTab.value === 'component') loadCompCfg();
      else if (settingsTab.value === 'rubrics') loadRubrics();
      else if (settingsTab.value === 'cache') loadCacheSettings();
    });

    return {
      site, component, sites, components, tab, settingsTab, tabs, jobsOpen, jobs, activeJobs,
      showRunTroubleshoot, showRunLoginModal, showRunRateLimitModal, runLoginUrl,
      runRateLimitBackoff, rateLimitCountdown, rateLimitWaiting, pendingRunAfterRateLimit,
      formatRateLimitWait, dismissRunRateLimitModal, retryRunAfterRateLimit,
      allStrategies, allFrameworks, runStrategies, runTestFiles, runVisibleFrameworks, runVisibleStrategies, logs,
      gen, run, risk, exp, cache,
      showModal, modalSite, modalComponent, modalComponents, modalNewSite, modalNewComponent,
      modalRenameSite, modalRenameComponent, modalError, modalMsg,
      onModalSiteChange, onModalComponentChange, confirmModal, openModal,
      modalCreateSite, modalRenameSiteAction, modalDeleteSite,
      modalCreateComponent, modalRenameComponentAction, modalDeleteComponent,
      HINTS, hintDismissed, dismissHint,
      runResults, runResultsLoading, expandedRunRows, toggleRunRow,
      compCfg, compCfgSaved, compCfgError, compCfgEmpty, INPUT_TYPES, PROMPT_TEMPLATE_HINT, PROMPT_MODEL_HINT, PROMPT_BODY_PLACEHOLDER,
      settingsSchema, compSettings, compSettingsInherited,
      settingMeta, settingLabel, formatSettingGlobal, onCompSettingInheritChange, toggleCompSettingSet,
      loadCompCfg, saveCompCfg, addInput, removeInput,
      companyRubricText, companySaved, companyError, companyGenerating, companyGenerateUrl,
      saveCompanyRubric, generateCompanyRubric,
      componentRubricText, componentRubricSaved, componentRubricError,
      componentRubricGenerating, componentGenerateUrl,
      saveComponentRubric, generateComponentRubric,
      cfg, cfgSaved, cfgError,
      BLOCKED_OPTIONS, COUNTRIES, CHANNELS, FETCH_METHODS,
      discoverJobId, discoverRunning, manualDiscoverJobId, manualDiscoverRunning,
      discoverTransport, apiDiscover, apiDiscoverRunning, llmApiPresets,
      applyApiPreset, onDiscoverTransportChange, onSettingsApiPreset, syncApiDiscoverFromCompCfg,
      apiNeedsAuth, apiAuthReady,
      authApiKeyHeader, authApiKeyQueryParam, authUseBearer,
      startApiDiscover,
      companyDiscoverJobId, companyDiscoverRunning, companyDiscoverDone,
      sampleRequestRunning,
      startCompanyDiscover, sendCompanyDiscoverEnter,
      loginJobId, loginRunning, loginUrl, authConfigured, authMode, authLoginChoice, authPublicSaving,
      authApiKey, authApiKeySaving, authSaving, authSaveError, pendingRunAfterLogin,
      chooseAuthRequired, chooseAuthApiKey, saveAuthApiKey, chooseAuthNotRequired, resetAuthSetup,
      startLogin, saveAuth, sendLoginEnter, confirmRunLogin, dismissRunLoginModal, onRunTroubleshoot,
      pretty, lineClass, activeOutput, runProgress, runProgressBarLabel, runProgressEtaText, riskTabProgressBarVisible, formatRunEta,
      submissionTransport, runShowsBrowserPreview,
      runPreviewSlots, initRunPreviewSlots, setRunPreviewSlot, clearRunPreview,
      runPreviewLightbox, openRunPreviewLightbox, closeRunPreviewLightbox,
      runBlockedInfo,
      onSiteChange, onComponentChange, loadContext, loadRunTestCatalog, onRunFrameworkChange, onRunStrategyChange, refreshRunTests,
      tmStrategy, tmStrategies, tmFramework, tmFrameworks, tmTestFiles, tmVisibleFrameworks, tmVisibleStrategies, tmFile, tmDirty, tmSaving, tmSaveMsg,
      tmEditingId, tmAddingMandate, tmNewPrompt, tmImportFile, tmImportName, tmImporting, tmImportMsg, showTmImportHelpModal,
      tmIsMultiTurnStrategy, tmEntryIsMultiTurn, tmPromptPreview, tmTurnLabel, tmEnsurePromptTurns, tmAddTurn, tmRemoveTurn,
      tmLoadStrategies, tmLoadFrameworks, tmOnStrategyChange, tmOnFrameworkChange, tmLoadFile, tmSave, tmDeletePrompt, tmStartAdd, tmConfirmAdd, tmMarkDirty,
      tmImportFileChanged, tmImportZeroShot,
      startGenerate, startDiscover, startManualDiscover, openComponentSettings, sendEnter,
      startRunTests, startSampleRequest, startRiskAssess, startExport, startClearCache,
      loadCacheSettings, saveCacheSettings, cacheSettingsSaving, cacheSettingsMsg,
      expResult, expPreview, expCreds, expCredsEdit, expCredsSaving, expCredsMsg,
      loadExpCreds, saveExpCreds, clearExpCreds,
      cancelJob, saveConfig, toggleBlocked,
    };
  }
}).mount('#app');

