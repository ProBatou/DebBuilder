import {mount} from 'svelte';
import App from './app/App.svelte';
import './styles.css';

mount(App, {target: document.getElementById('app')});
