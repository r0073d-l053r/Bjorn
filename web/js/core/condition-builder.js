/**
 * condition-builder.js - Visual block-based condition editor for triggers.
 * Produces/consumes JSON condition trees with AND/OR groups + leaf conditions.
 */
import { el, empty } from './dom.js';

// Condition source definitions (drives the parameter UI)
const SOURCES = {
  action_result: {
    label: 'Action Result',
    params: [
      { key: 'action', type: 'text', placeholder: 'e.g. scanning', label: 'Action' },
      { key: 'check', type: 'select', choices: ['eq', 'neq'], label: 'Check' },
      { key: 'value', type: 'select', choices: ['success', 'failed'], label: 'Value' },
    ],
  },
  hosts_with_port: {
    label: 'Hosts with Port',
    params: [
      { key: 'port', type: 'number', placeholder: '22', label: 'Port' },
      { key: 'check', type: 'select', choices: ['gt', 'lt', 'eq', 'gte', 'lte'], label: 'Op' },
      { key: 'value', type: 'number', placeholder: '0', label: 'Count' },
    ],
  },
  hosts_alive: {
    label: 'Hosts Alive',
    params: [
      { key: 'check', type: 'select', choices: ['gt', 'lt', 'eq', 'gte', 'lte'], label: 'Op' },
      { key: 'value', type: 'number', placeholder: '0', label: 'Count' },
    ],
  },
  cred_found: {
    label: 'Credentials Found',
    params: [
      { key: 'service', type: 'text', placeholder: 'e.g. ssh, ftp', label: 'Service' },
    ],
  },
  has_vuln: {
    label: 'Has Vulnerabilities',
    params: [],
  },
  db_count: {
    label: 'DB Row Count',
    params: [
      { key: 'table', type: 'select', choices: ['hosts', 'creds', 'vulnerabilities', 'services'], label: 'Table' },
      { key: 'check', type: 'select', choices: ['gt', 'lt', 'eq', 'gte', 'lte'], label: 'Op' },
      { key: 'value', type: 'number', placeholder: '0', label: 'Count' },
    ],
  },
  time_after: {
    label: 'Time After',
    params: [
      { key: 'hour', type: 'number', placeholder: '9', label: 'Hour (0-23)', min: 0, max: 23 },
      { key: 'minute', type: 'number', placeholder: '0', label: 'Minute (0-59)', min: 0, max: 59 },
    ],
  },
  time_before: {
    label: 'Time Before',
    params: [
      { key: 'hour', type: 'number', placeholder: '18', label: 'Hour (0-23)', min: 0, max: 23 },
      { key: 'minute', type: 'number', placeholder: '0', label: 'Minute (0-59)', min: 0, max: 59 },
    ],
  },
};

/**
 * Build a condition editor inside a container element.
 * @param {HTMLElement} container - DOM element to render into
 * @param {object|null} initial - Initial condition JSON tree (null = empty AND group)
 */
export function buildConditionEditor(container, initial = null) {
  empty(container);
  container.classList.add('cond-editor');
  const root = initial || { type: 'group', op: 'AND', children: [] };
  container.appendChild(_renderNode(root));
}

/**
 * Read the current condition tree from the DOM.
 * @param {HTMLElement} container - The editor container
 * @returns {object} JSON condition tree
 */
export function getConditions(container) {
  const rootEl = container.querySelector('.cond-group, .cond-block');
  if (!rootEl) return null;
  return _readNode(rootEl);
}

// --- Internal rendering ---

function _renderNode(node) {
  if (node.type === 'group') return _renderGroup(node);
  return _renderCondition(node);
}

function _renderGroup(node) {
  const op = (node.op || 'AND').toUpperCase();
  const childContainer = el('div', { class: 'cond-children' });

  // Render existing children
  (node.children || []).forEach(child => {
    childContainer.appendChild(_wrapDeletable(_renderNode(child)));
  });

  const opToggle = el('select', { class: 'cond-op-toggle', 'data-op': op }, [
    el('option', { value: 'AND', selected: op === 'AND' ? '' : null }, ['AND']),
    el('option', { value: 'OR', selected: op === 'OR' ? '' : null }, ['OR']),
  ]);
  opToggle.value = op;
  opToggle.addEventListener('change', () => {
    group.dataset.op = opToggle.value;
    group.classList.toggle('cond-group-or', opToggle.value === 'OR');
    group.classList.toggle('cond-group-and', opToggle.value === 'AND');
  });

  const addCondBtn = el('button', {
    class: 'cond-add-btn',
    type: 'button',
    onClick: () => {
      const newCond = { type: 'condition', source: 'action_result', action: '', check: 'eq', value: 'success' };
      childContainer.appendChild(_wrapDeletable(_renderCondition(newCond)));
    },
  }, ['+ Condition']);

  const addGroupBtn = el('button', {
    class: 'cond-add-btn cond-add-group-btn',
    type: 'button',
    onClick: () => {
      const newGroup = { type: 'group', op: 'AND', children: [] };
      childContainer.appendChild(_wrapDeletable(_renderGroup(newGroup)));
    },
  }, ['+ Group']);

  const group = el('div', {
    class: `cond-group cond-group-${op.toLowerCase()}`,
    'data-type': 'group',
    'data-op': op,
  }, [
    el('div', { class: 'cond-group-header' }, [opToggle]),
    childContainer,
    el('div', { class: 'cond-group-actions' }, [addCondBtn, addGroupBtn]),
  ]);

  return group;
}

function _renderCondition(node) {
  const source = node.source || 'action_result';
  const paramsContainer = el('div', { class: 'cond-params' });

  const sourceSelect = el('select', { class: 'cond-source-select' });
  Object.entries(SOURCES).forEach(([key, def]) => {
    const opt = el('option', { value: key, selected: key === source ? '' : null }, [def.label]);
    sourceSelect.appendChild(opt);
  });
  sourceSelect.value = source;

  // Build params for current source
  _buildParams(paramsContainer, source, node);

  sourceSelect.addEventListener('change', () => {
    const newSource = sourceSelect.value;
    block.dataset.source = newSource;
    _buildParams(paramsContainer, newSource, {});
  });

  const block = el('div', {
    class: 'cond-block',
    'data-type': 'condition',
    'data-source': source,
  }, [sourceSelect, paramsContainer]);

  return block;
}

function _buildParams(container, source, data) {
  empty(container);
  const def = SOURCES[source];
  if (!def) return;

  def.params.forEach(p => {
    const val = data[p.key] !== undefined ? data[p.key] : (p.placeholder || '');
    let input;

    if (p.type === 'select') {
      input = el('select', { class: 'cond-param-input', 'data-key': p.key });
      (p.choices || []).forEach(c => {
        const opt = el('option', { value: c, selected: String(c) === String(data[p.key] || '') ? '' : null }, [c]);
        input.appendChild(opt);
      });
      if (data[p.key] !== undefined) input.value = String(data[p.key]);
    } else if (p.type === 'number') {
      input = el('input', {
        type: 'number',
        class: 'cond-param-input',
        'data-key': p.key,
        value: data[p.key] !== undefined ? String(data[p.key]) : '',
        placeholder: p.placeholder || '',
        min: p.min !== undefined ? String(p.min) : undefined,
        max: p.max !== undefined ? String(p.max) : undefined,
      });
    } else {
      input = el('input', {
        type: 'text',
        class: 'cond-param-input',
        'data-key': p.key,
        value: data[p.key] !== undefined ? String(data[p.key]) : '',
        placeholder: p.placeholder || '',
      });
    }

    container.appendChild(
      el('label', { class: 'cond-param-label' }, [
        el('span', { class: 'cond-param-name' }, [p.label]),
        input,
      ])
    );
  });
}

function _wrapDeletable(nodeEl) {
  const wrapper = el('div', { class: 'cond-item-wrapper' }, [
    nodeEl,
    el('button', {
      class: 'cond-delete-btn',
      type: 'button',
      title: 'Remove',
      onClick: () => wrapper.remove(),
    }, ['\u00d7']),
  ]);
  return wrapper;
}

// --- Read DOM -> JSON ---

function _readNode(nodeEl) {
  const type = nodeEl.dataset.type;
  if (type === 'group') return _readGroup(nodeEl);
  if (type === 'condition') return _readCondition(nodeEl);

  // Check if it's a wrapper
  const inner = nodeEl.querySelector('.cond-group, .cond-block');
  if (inner) return _readNode(inner);
  return null;
}

function _readGroup(groupEl) {
  const op = groupEl.dataset.op || 'AND';
  const children = [];
  const childrenContainer = groupEl.querySelector('.cond-children');
  if (childrenContainer) {
    for (const wrapper of childrenContainer.children) {
      const inner = wrapper.querySelector('.cond-group, .cond-block');
      if (inner) {
        const child = _readNode(inner);
        if (child) children.push(child);
      }
    }
  }
  return { type: 'group', op: op.toUpperCase(), children };
}

function _readCondition(blockEl) {
  const source = blockEl.dataset.source || 'action_result';
  const result = { type: 'condition', source };

  const inputs = blockEl.querySelectorAll('.cond-param-input');
  inputs.forEach(input => {
    const key = input.dataset.key;
    if (!key) return;
    let val = input.value;
    // Auto-cast numbers
    if (input.type === 'number' && val !== '') {
      val = Number(val);
    }
    result[key] = val;
  });

  return result;
}
