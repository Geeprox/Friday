'use strict';
// Fixture dependency loading; the actual display-copy implementation is retained.
const vm = require('node:vm'), fs = require('node:fs'), path = require('node:path');
const original = vm.runInContext;
function prepare(context) {
  context.AbortController ??= AbortController;
  context.TextEncoder ??= TextEncoder;
  context.TextDecoder ??= TextDecoder;
  context.URL ??= URL;
  let document = context.document;
  if (!document?.createElement) {
    if (typeof context.el !== 'function') return;
    document = context.document = {createElement: context.el, querySelectorAll: () => [], documentElement: {}};
    context.MutationObserver = class {observe() {}};
    context.queueMicrotask = queueMicrotask;
    original(fs.readFileSync(path.join(__dirname, '../homeplane/static/copy-layout.js'), 'utf8'), context);
  }
  document.addEventListener ??= (type,listener) => {document.events ??= {};document.events[type]=listener;};
  if (context.FridayCopyLayout) {
    Object.assign(context, {copyName: context.FridayCopyLayout.name, withCopy: context.FridayCopyLayout.withCopy,
                           copyLayout: context.FridayCopyLayout});
    return;
  }
  const Element = document.createElement('div').constructor;
  if (!Object.getOwnPropertyDescriptor(Element.prototype, 'textContent')?.get) {
    context.MutationObserver ??= class {observe() {}};
    context.queueMicrotask ??= () => {};
    original(fs.readFileSync(path.join(__dirname, '../homeplane/static/copy-layout.js'), 'utf8'), context);
    Object.assign(context, {copyName: context.FridayCopyLayout.name, withCopy: context.FridayCopyLayout.withCopy,
                           copyLayout: context.FridayCopyLayout});
    return;
  }
  const root = document.body || document.getElementById?.('content') || document.createElement('body');
  root.dataset ??= {};
  root.dataset.consoleSurface ??= 'legacy';
  Object.assign(context, require('./copy_layout_dom.cjs').install(Element, document, root));
  original(fs.readFileSync(path.join(__dirname, '../homeplane/static/copy-layout.js'), 'utf8'), context);
  Object.assign(context, {copyName: context.FridayCopyLayout.name, withCopy: context.FridayCopyLayout.withCopy,
                         copyLayout: context.FridayCopyLayout});
}
vm.runInContext = (code, context, ...rest) => {prepare(context); return original(code, context, ...rest);};
module.exports = {prepare};
