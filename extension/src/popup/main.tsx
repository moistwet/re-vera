/**
 * Popup entry point. Mounts the React tree and nothing else — every side effect
 * lives in App, and none of them run before the reader opens the popup.
 */

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import App from './App'
import './theme.css'

const container = document.getElementById('root')
if (!container) throw new Error('Re-Vera popup: #root is missing from index.html')

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
