/// <reference types="vite/client" />

// Without this, TypeScript rejects the side-effect import of index.css in main.tsx:
// it has no declaration for a .css module and reports TS2882.
