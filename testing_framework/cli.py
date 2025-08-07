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

from .core.hierarchical_runner import HierarchicalTestRunner
from .core.base_test import TestCategory

# Use hierarchical runner as the default
TestRunner = HierarchicalTestRunner


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
@click.option('--clear', is_flag=True, help='Clear test_results directory before running tests')
@click.pass_context
async def run_all(ctx, clear):
    """Run all available tests with dependency resolution."""
    if clear:
        click.echo("🗑️  Clearing test_results directory...")
    click.echo("🚀 Running all tests with dependency resolution...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner(clear_results=clear)
    results = await runner.run_all_tests(_create_progress_callback())
    _print_results(results)


@cli.command()
@click.argument('category', type=click.Choice([cat.value for cat in TestCategory]))
@click.option('--clear', is_flag=True, help='Clear test_results directory before running tests')
@click.pass_context
async def run_category(ctx, category, clear):
    """Run tests in a specific category with dependency resolution."""
    cat_enum = TestCategory(category)
    if clear:
        click.echo("🗑️  Clearing test_results directory...")
    click.echo(f"🎯 Running {category} tests with dependency resolution...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner(clear_results=clear)
    results = await runner.run_tests_by_category(cat_enum, _create_progress_callback())
    _print_results(results)


@cli.command()
@click.argument('tags', nargs=-1, required=True)
@click.option('--clear', is_flag=True, help='Clear test_results directory before running tests')
@click.pass_context
async def run_tags(ctx, tags, clear):
    """Run tests with specific tags."""
    if clear:
        click.echo("🗑️  Clearing test_results directory...")
    click.echo(f"🏷️  Running tests with tags: {', '.join(tags)}...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner(clear_results=clear)
    results = await runner.run_tests_by_tags(set(tags), _create_progress_callback())
    _print_results(results)


@cli.command()
@click.argument('names', nargs=-1, required=True)
@click.option('--clear', is_flag=True, help='Clear test_results directory before running tests')
@click.pass_context
async def run_tests(ctx, names, clear):
    """Run specific tests by name."""
    if clear:
        click.echo("🗑️  Clearing test_results directory...")
    click.echo(f"📝 Running tests: {', '.join(names)}...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner(clear_results=clear)
    results = await runner.run_tests_by_names(list(names), _create_progress_callback())
    _print_results(results)


@cli.command()
@click.argument('pattern')
@click.option('--clear', is_flag=True, help='Clear test_results directory before running tests')
@click.pass_context
async def run_pattern(ctx, pattern, clear):
    """Run tests matching a name pattern."""
    if clear:
        click.echo("🗑️  Clearing test_results directory...")
    click.echo(f"🔍 Running tests matching pattern: {pattern}...")
    click.echo("🔗 Starting shared CLI connection...", nl=False)
    runner = TestRunner(clear_results=clear)
    results = await runner.run_tests_by_pattern(pattern, _create_progress_callback())
    _print_results(results)


@cli.command()
@click.option('--clear', is_flag=True, help='Clear test_results directory before running tests')
@click.pass_context
async def run_hierarchical(ctx, clear):
    """Run all tests with hierarchical dependency resolution."""
    if clear:
        click.echo("🗑️  Clearing test_results directory...")
    click.echo("🚀 Running tests with dependency resolution...")
    click.echo("📋 Analyzing test dependencies...")
    
    runner = HierarchicalTestRunner(clear_results=clear)
    results = await runner.run_all_tests(_create_progress_callback())
    _print_results(results)
    
    # Show dependency information
    if results.get('results'):
        skipped = [r for r in results['results'] if 'Skipped:' in r.get('message', '')]
        if skipped:
            click.echo(f"\n⏭️  Skipped Tests ({len(skipped)}):")
            for result in skipped:
                click.echo(f"  • {result['test_name']}: {result['message']}")


@cli.command() 
@click.argument('names', nargs=-1, required=True)
@click.pass_context
async def run_hierarchical_tests(ctx, names):
    """Run specific tests with dependency resolution."""
    click.echo(f"📝 Running tests with dependencies: {', '.join(names)}...")
    click.echo("📋 Analyzing test dependencies...")
    
    runner = HierarchicalTestRunner()
    results = await runner.run_tests_by_names(list(names), _create_progress_callback())
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
            click.echo(f"\r[{current}/{total}] {'✅' if result.success else '❌'} {test.name} ({result.duration:.2f}s)", nl=True)
            if not result.success and result.message:
                click.echo(f"    └─ {click.style(result.message, fg='red')}")
    
    return progress_callback


def _print_results(results):
    """Print test results summary with stylish stats formatting."""
    if not results.get('results'):
        click.echo("❌ No tests were run")
        return
        
    stats = results['statistics']
    duration = results['run_info']['duration']
    
    # Stylish header
    click.echo("\n" + "="*60)
    click.echo(f"{'📊 TEST RESULTS SUMMARY':^60}")
    click.echo("="*60)
    
    # Main stats with better formatting
    click.echo(f"  📋 Total Tests:    {click.style(str(stats['total_tests']), fg='cyan', bold=True)}")
    click.echo(f"  ⏱️  Total Duration: {click.style(f'{duration:.2f}s', fg='blue', bold=True)}")
    click.echo(f"  ✅ Passed:        {click.style(str(stats['passed']), fg='green', bold=True)}")
    click.echo(f"  ❌ Failed:        {click.style(str(stats['failed']), fg='red', bold=True)}")
    
    # Success rate with color coding
    success_rate_text = f"{stats['success_rate']:.2f}%"
    success_rate_color = 'green' if stats['success_rate'] > 80 else 'yellow' if stats['success_rate'] > 50 else 'red'
    click.echo(f"  📈 Success Rate:  {click.style(success_rate_text, fg=success_rate_color, bold=True)}")
    
    # Individual test stats with timing
    click.echo(f"\n{'⚡ PERFORMANCE BREAKDOWN':^60}")
    click.echo("-"*60)
    
    # Show timing stats for each test
    for result in results['results']:
        status_icon = "✅" if result['success'] else "❌"
        test_name = result['test_name'].replace('_test', '').replace('_', ' ').title()
        duration_text = f"{result['duration']:.2f}s"
        duration_color = 'green' if result['duration'] < 5 else 'yellow' if result['duration'] < 10 else 'red'
        
        click.echo(f"  {status_icon} {test_name:<30} {click.style(duration_text, fg=duration_color)}")
        
        # Show additional stats if available
        if result.get('artifacts') and isinstance(result['artifacts'], dict):
            # Check both direct artifacts and nested timings/stats
            all_stats = {}
            
            # Add direct artifacts
            for key, value in result['artifacts'].items():
                if isinstance(value, (int, float)):
                    all_stats[key] = value
            
            # Add nested timings
            if 'timings' in result['artifacts'] and isinstance(result['artifacts']['timings'], dict):
                for key, value in result['artifacts']['timings'].items():
                    if isinstance(value, (int, float)):
                        all_stats[key] = value
            
            # Add nested stats
            if 'stats' in result['artifacts'] and isinstance(result['artifacts']['stats'], dict):
                for key, value in result['artifacts']['stats'].items():
                    if isinstance(value, (int, float)):
                        all_stats[key] = value
            
            # Display all stats
            for key, value in all_stats.items():
                # Format based on the value and key name
                if 'ms' in key.lower() or value > 100:
                    formatted_value = f"{value:.2f}ms"
                else:
                    formatted_value = f"{value:.2f}s"
                
                # Clean up key name for display
                display_key = key.replace('_', ' ').replace('time ms', 'time').title()
                click.echo(f"    └─ {display_key}: {click.style(formatted_value, fg='blue')}")
    
    click.echo(f"\n📂 Results saved to: {click.style(results['run_info']['run_directory'], fg='blue', underline=True)}")
    
    # Show failed tests summary if any
    failed_tests = [r for r in results['results'] if not r['success']]
    if failed_tests:
        click.echo(f"\n{'❌ FAILED TESTS DETAILS':^60}")
        click.echo("-"*60)
        for result in failed_tests:
            click.echo(f"  • {click.style(result['test_name'], fg='red', bold=True)}")
            if result.get('message'):
                # Handle multi-line error messages
                message_lines = result['message'].split('\n')
                for i, line in enumerate(message_lines):
                    prefix = "    └─ " if i == 0 else "       "
                    click.echo(f"{prefix}{click.style(line, fg='red')}")
                
                # Add note about trace viewer
                click.echo(f"       {click.style('🔍 Trace viewer should open automatically for investigation', fg='yellow')}")
    
    # Summary footer
    click.echo("="*60)


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
run_hierarchical.callback = async_command(run_hierarchical.callback)
run_hierarchical_tests.callback = async_command(run_hierarchical_tests.callback)


if __name__ == '__main__':
    cli()