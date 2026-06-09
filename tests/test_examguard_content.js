const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'apps', 'examguard', 'extension', 'content.js'),
  'utf8'
);

let storageChanged;
const documentListeners = [];
const bodyChildren = [];
const document = {
  body: {
    appendChild(node) {
      bodyChildren.push(node);
    }
  },
  addEventListener(name) {
    documentListeners.push(name);
  },
  createElement() {
    return {
      id: '',
      innerHTML: '',
      style: { cssText: '' },
      remove() {
        const index = bodyChildren.indexOf(this);
        if (index >= 0) bodyChildren.splice(index, 1);
      }
    };
  },
  getElementById(id) {
    return bodyChildren.find(node => node.id === id) || null;
  }
};

const context = {
  console,
  document,
  globalThis: null,
  chrome: {
    storage: {
      local: {
        get(_keys, callback) {
          callback({ examActive: false });
        }
      },
      onChanged: {
        addListener(callback) {
          storageChanged = callback;
        }
      }
    },
    runtime: {
      sendMessage() {}
    }
  }
};
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context);

assert.equal(documentListeners.length, 0);
storageChanged(
  { examActive: { oldValue: false, newValue: true } },
  'local'
);
assert.ok(documentListeners.includes('contextmenu'));
assert.equal(bodyChildren.filter(node => node.id === 'examguard-badge').length, 1);

storageChanged(
  { examActive: { oldValue: true, newValue: false } },
  'local'
);
assert.equal(context.__examGuardActive, false);
assert.equal(bodyChildren.filter(node => node.id === 'examguard-badge').length, 0);

storageChanged(
  { examActive: { oldValue: false, newValue: true } },
  'local'
);
assert.equal(context.__examGuardActive, true);
assert.equal(bodyChildren.filter(node => node.id === 'examguard-badge').length, 1);

const listenerCount = documentListeners.length;
vm.runInContext(source, context);
assert.equal(documentListeners.length, listenerCount);
assert.equal(bodyChildren.filter(node => node.id === 'examguard-badge').length, 1);

console.log('ExamGuard content activation test passed');
