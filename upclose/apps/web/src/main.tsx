import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './styles.css';
import './ui/base.css';
import './ui/app.css';

const root = document.getElementById('root');
if (!root) throw new Error('UpClose root element is missing.');
createRoot(root).render(<StrictMode><App /></StrictMode>);
