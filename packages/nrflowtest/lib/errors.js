'use strict';
class LoadError extends Error {
  constructor(message) { super(message); this.name = 'LoadError'; }
}
module.exports = { LoadError };
