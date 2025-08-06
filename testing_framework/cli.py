"""Command-line interface for the testing framework."""

import asyncio
import click
import logging
import sys
import os
from pathlib import Path

# Load environment variables from .env file if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .core.runner import TestRunner
from .core.base_test import TestCategory


def setup_logging(debug: bool = False):
    """Setup logging configuration - logs only to files, not console."""
    level = logging.DEBUG if debug else logging.INFO
    
    # Only log to files, not to console
    # Create a null handler to prevent any console output from framework logs
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.NullHandler()]
    )
    
    # If debug is enabled, we can optionally add a file handler here
    if debug:
        # Create debug log file in current directory
        debug_handler = logging.FileHandler('framework_debug.log')
        debug_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        ))
        logging.getLogger().addHandler(debug_handler)


@click.group()
@click.option('--debug', is_flag=True, help='Enable debug logging')
@click.pass_context
def cli(ctx, debug):
    """Modular Testing Framework for Portacode"""
    ctx.ensure_object(dict)
    ctx.obj['debug'] = debug
    setup_logging(debug)


@cli.command()
@click.pass_context
async def list_tests(ctx):
    """List all available tests."""
    runner = TestRunner()
    info = runner.list_available_tests()
    
    click.echo(f"📋 Found {click.style(str(info['total_tests']), fg='green')} tests")
    click.echo(f"Categories: {click.style(', '.join([cat.value for cat in info['categories']]), fg='blue')}")
    if info['tags']:
        click.echo(f"Tags: {click.style(', '.join(info['tags']), fg='cyan')}")
    
    click.echo("\n📝 Available Tests:")
    for name, test_info in info['tests'].items():
        click.echo(f"  • {click.style(name, fg='yellow')}")
        click.echo(f"    Category: {click.style(test_info['category'], fg='blue')}")
        click.echo(f"    Description: {test_info['description']}")
        if test_info['tags']:
            click.echo(f"    Tags: {click.style(', '.join(test_info['tags']), fg='cyan')}")
        click.echo()


@cli.command()
@click.pass_context
async def run_all(ctx):
    """Run all available tests."""
    click.echo("🚀 Running all tests...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner()
    results = await runner.run_all_tests(_create_progress_callback())
    _print_results(results)


@cli.command()
@click.argument('category', type=click.Choice([cat.value for cat in TestCategory]))
@click.pass_context
async def run_category(ctx, category):
    """Run tests in a specific category."""
    cat_enum = TestCategory(category)
    click.echo(f"🎯 Running {category} tests...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner()
    results = await runner.run_tests_by_category(cat_enum, _create_progress_callback())
    _print_results(results)


@cli.command()
@click.argument('tags', nargs=-1, required=True)
@click.pass_context
async def run_tags(ctx, tags):
    """Run tests with specific tags."""
    click.echo(f"🏷️  Running tests with tags: {', '.join(tags)}...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner()
    results = await runner.run_tests_by_tags(set(tags), _create_progress_callback())
    _print_results(results)


@cli.command()
@click.argument('names', nargs=-1, required=True)
@click.pass_context
async def run_tests(ctx, names):
    """Run specific tests by name."""
    click.echo(f"📝 Running tests: {', '.join(names)}...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner()
    results = await runner.run_tests_by_names(list(names), _create_progress_callback())
    _print_results(results)


@cli.command()
@click.argument('pattern')
@click.pass_context
async def run_pattern(ctx, pattern):
    """Run tests matching a name pattern."""
    click.echo(f"🔍 Running tests matching pattern: {pattern}...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner()
    results = await runner.run_tests_by_pattern(pattern, _create_progress_callback())
    _print_results(results)


def _create_progress_callback():
    """Create a progress callback for clean console output."""
    cli_connected_shown = False
    
    def progress_callback(event, test, current, total, result=None):
        nonlocal cli_connected_shown
        
        if event == 'start':
            # Show CLI connected message only once
            if not cli_connected_shown:
                click.echo("\r🔗 Shared CLI connection established ✅")
                cli_connected_shown = True
            # Clean one-line output for test start  
            click.echo(f"[{current}/{total}] 🔄 {test.name}", nl=False)
        elif event == 'complete' and result:
            # Clear the line and show result
            click.echo(f"\r[{current}/{total}] {'✅' if result.success else '❌'} {test.name} ({result.duration:.1f}s)", nl=True)
            if not result.success and result.message:
                click.echo(f"    └─ {click.style(result.message, fg='red')}")
    
    return progress_callback


def _print_results(results):
    """Print test results summary."""
    if not results.get('results'):
        click.echo("❌ No tests were run")
        return
        
    stats = results['statistics']
    duration = results['run_info']['duration']
    
    click.echo(f"\n📊 Test Results Summary:")
    click.echo(f"  Total: {stats['total_tests']} | Duration: {duration:.1f}s")
    click.echo(f"  ✅ Passed: {click.style(str(stats['passed']), fg='green')}")
    click.echo(f"  ❌ Failed: {click.style(str(stats['failed']), fg='red')}")
    success_rate_text = f"{stats['success_rate']:.1f}%"
    success_rate_color = 'green' if stats['success_rate'] > 80 else 'yellow' if stats['success_rate'] > 50 else 'red'
    click.echo(f"  📈 Success Rate: {click.style(success_rate_text, fg=success_rate_color)}")
    
    click.echo(f"\n📂 Results: {click.style(results['run_info']['run_directory'], fg='blue', underline=True)}")
    
    # Show failed tests summary
    failed_tests = [r for r in results['results'] if not r['success']]
    if failed_tests:
        click.echo(f"\n❌ Failed Tests ({len(failed_tests)}):")
        for result in failed_tests:
            click.echo(f"  • {result['test_name']}")


# Async command wrapper
def async_command(f):
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper


# Convert async commands
list_tests.callback = async_command(list_tests.callback)
run_all.callback = async_command(run_all.callback)
run_category.callback = async_command(run_category.callback)
run_tags.callback = async_command(run_tags.callback)
run_tests.callback = async_command(run_tests.callback)
run_pattern.callback = async_command(run_pattern.callback)


if __name__ == '__main__':
    cli()