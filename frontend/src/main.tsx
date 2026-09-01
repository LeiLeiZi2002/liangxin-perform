import { QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'

import App from './App'
import { queryClient } from './app/queryClient'
import './styles/tokens.css'
import './styles/global.css'

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <App />
  </QueryClientProvider>,
)
