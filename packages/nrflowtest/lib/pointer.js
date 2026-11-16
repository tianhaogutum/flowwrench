'use strict';
// JSON Pointer string syntax: no URI fragments, expressions or wildcard expansion.
function pointerTokens(pointer) {
  if (pointer === '') return [];
  if (typeof pointer !== 'string' || !pointer.startsWith('/') || /~(?:[^01]|$)/u.test(pointer)) return null;
  return pointer.slice(1).split('/').map(token => token.replace(/~[01]/g, escape => escape === '~1' ? '/' : '~'));
}
function resolvePointer(value, pointer) {
  for (const token of pointerTokens(pointer)) {
    if (value === null || typeof value !== 'object' ||
      (Array.isArray(value) && !/^(0|[1-9][0-9]*)$/.test(token)) || !Object.hasOwn(value, token)) return { found: false };
    value = value[token];
  }
  return { found: true, value };
}
module.exports = { pointerTokens, resolvePointer };
