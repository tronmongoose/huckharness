import "@testing-library/jest-dom";

// jsdom has no canvas and logs "not implemented" on getContext. A null context
// is what a browser without 2D canvas gives; tests that draw stub it themselves.
HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
