import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { SamplePage } from './SamplePage';
import '../ui/base.css';
import '../ui/kit.css';
import './sample.css';

const root = document.getElementById('root');
if (!root) throw new Error('UpClose sample root is missing.');
createRoot(root).render(
  <StrictMode>
    <SamplePage appOrigin={import.meta.env.VITE_APP_ORIGIN || null} repoUrl={import.meta.env.VITE_REPO_URL} />
  </StrictMode>,
);
