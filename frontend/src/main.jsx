import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { Toaster } from 'react-hot-toast'
import './index.css'
import "./KAIROS/styles/index.css";
import App from './App.jsx'
// TEMPORARY: blink/reload investigation instrumentation. Installs global
// error / rejection / unload / reload capture + the main-process bridge as
// early as possible. Remove once the root cause is confirmed.
import { installBlinkGlobals } from './lib/blinkLog'

installBlinkGlobals()

createRoot(document.getElementById('root')).render(
  <StrictMode>

    {/* 🔥 Toast Notifications */}
    <Toaster 
      position="top-right"
      toastOptions={{
        style: {
          borderRadius: '10px',
          background: '#111827',
          color: '#fff',
        },
      }}
    />

    <App />

  </StrictMode>,
)