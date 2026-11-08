import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './ui/base.css';
import './ui/kit.css';
import './ui/app.css';

const root = document.getElementById('root');
if (!root) throw new Error('UpClose root element is missing.');
createRoot(root).render(<StrictMode><App /></StrictMode>);
