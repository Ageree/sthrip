"""
Sthrip - Anonymous Payments for AI Agents
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="sthrip",
    version="2.0.0",
    author="Sthrip Team",
    author_email="hello@sthrip.io",
    description="Anonymous payments for AI Agents via Monero",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/sthrip/sthrip",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Office/Business :: Financial",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.8",
    install_requires=[
        # Core
        "requests>=2.28.0",
        "pydantic>=2.0.0",
        
        # Database
        "sqlalchemy>=2.0.0",
        "psycopg2-binary>=2.9.0",
        
        # Cache & Queue
        "redis>=4.5.0",
        
        # API
        "fastapi>=0.100.0",
        "uvicorn[standard]>=0.23.0",
        "python-multipart>=0.0.6",
        
        # Async
        "aiohttp>=3.8.0",
        "asyncio-mqtt>=0.13.0",
        
        # Crypto
        "pycryptodome>=3.18.0",
        "hashlib2>=1.1.0",
        
        # Monitoring
        "psutil>=5.9.0",
        
        # Optional MCP
        "mcp>=0.1.0; extra == 'mcp'",
    ],
    extras_require={
        "mcp": ["mcp>=0.1.0"],
        "dev": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.21.0",
            "black>=23.0.0",
            "flake8>=6.0.0",
            "mypy>=1.0.0",
        ],
        "all": [
            "mcp>=0.1.0",
            "pytest>=7.0.0",
            "black>=23.0.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "sthrip=sthrip.cli.main:main",
            "sthrip-api=sthrip.api.main_v2:main",
            "sthrip-mcp=sthrip.integrations.mcp_server:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)
