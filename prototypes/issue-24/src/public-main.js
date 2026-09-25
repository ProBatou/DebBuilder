import PublicRepository from './PublicRepository.svelte';
import './public.css';
import {mount} from 'svelte';

mount(PublicRepository,{target:document.getElementById('public-app')});
